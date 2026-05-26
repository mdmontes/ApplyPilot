"""Apply orchestration: acquire jobs, spawn Gemini Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Gemini Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.llm import get_client
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)
from applypilot.apply.check_propulatedapp_prompt import check_prepopulated_app

logger = logging.getLogger(__name__)

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            }
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, custom_sql: str | None = None) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        custom_sql: Optional custom SQL query to restrict job selection.

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url,
                       fit_score, location, full_description,
                       application_schema
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            if custom_sql:
                query = f"""
                    WITH custom_subset AS ({custom_sql})
                    SELECT url, title, site, application_url,
                           fit_score, location, full_description,
                           application_schema
                    FROM custom_subset
                    WHERE apply_status IS NULL
                      AND fit_score >= ?
                      AND application_url IS NOT NULL
                    ORDER BY fit_score DESC, url
                    LIMIT 1
                """
                row = conn.execute(query, [min_score]).fetchone()
            else:
                query = """
                    SELECT url, title, site, application_url,
                           fit_score, location, full_description,
                           application_schema
                    FROM jobs
                    WHERE apply_status IS NULL
                      AND fit_score >= ?
                    ORDER BY fit_score DESC, url
                    LIMIT 1
                """
                row = conn.execute(query, [min_score]).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                duration_ms: int | None = None,
                task_id: str | None = None,
                application_details: str | None = None,
                application_prepopulated: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           application_details = ?,
                           application_prepopulated = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, application_details, application_prepopulated, url))
    else:
        # All failures are now treated as final (no retries)
        conn.execute("""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = 99, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           application_details = ?,
                           application_prepopulated = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, application_details, application_prepopulated, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = config.DEFAULTS["model_gemini"], worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Gemini CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Generate prompt
    prompt = prompt_mod.build_prompt(job=job)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def prepopulate_app(job: dict, profile: dict) -> list[dict]:
    """Compare application_schema against profile.json to create a prepopulated map.
    
    Returns a list of question maps with applicant_answer.
    """
    schema_str = job.get("application_schema")
    if not schema_str:
        return []
        
    try:
        schema = json.loads(schema_str)
    except Exception:
        return []

    # Flatten questions from different sections
    all_questions = []
    if isinstance(schema, list):
        all_questions = schema
    elif isinstance(schema, dict):
        if "questions" in schema and isinstance(schema["questions"], list):
            all_questions.extend(schema["questions"])
        if "compliance" in schema and isinstance(schema["compliance"], list):
            for comp in schema["compliance"]:
                if "questions" in comp and isinstance(comp["questions"], list):
                    all_questions.extend(comp["questions"])
        if "demographic_questions" in schema and isinstance(schema["demographic_questions"], list):
            all_questions.extend(schema["demographic_questions"])
        
        # Fallback: if dict has no known keys but has 'label' or 'id', it might be a single question
        if not all_questions and ("label" in schema or "id" in schema or "fields" in schema):
            all_questions = [schema]

    prepopulated = []
    personal = profile.get("personal", {})
    work_auth = profile.get("work_authorization", {})
    eeo = profile.get("eeo_voluntary", {})
    exp = profile.get("experience", {})
    comp = profile.get("compensation", {})

    for q in all_questions:
        if not isinstance(q, dict): continue
        
        label = q.get("label", "")
        required = q.get("required", False)
        fields = q.get("fields", [])
        
        # Normalize: if no fields list, treat the question itself as a field
        if not fields:
            fid = q.get("id") or q.get("name")
            if fid:
                fields = [{"name": fid, "type": q.get("type", "input_text")}]
                # Preserve existing answer if any
                if "applicant_answer" in q:
                    fields[0]["applicant_answer"] = q["applicant_answer"]
            elif label:
                # Last resort: generate a synthetic ID from label
                fid = "field_" + "".join(filter(str.isalnum, label.lower()))[:20]
                fields = [{"name": fid, "type": "input_text"}]
        
        for f in fields:
            if not isinstance(f, dict): continue
            
            fid = f.get("name", f.get("id", ""))
            ftype = f.get("type", "")
            
            # Start with existing answer if available, else default to not found
            answer = f.get("applicant_answer", q.get("applicant_answer", "answer_not_found"))
            
            # Basic mapping logic (only overwrite if we don't already have a real answer)
            if answer in ("answer_not_found", "do_not_fill", "", None):
                low_label = label.lower()
                if "cover letter" in low_label:
                    answer = "do_not_fill"
                elif "first name" in low_label:
                    answer = personal.get("full_name", "").split()[0] if personal.get("full_name") else "answer_not_found"
                elif "last name" in low_label:
                    name_parts = personal.get("full_name", "").split()
                    answer = name_parts[-1] if len(name_parts) > 1 else "answer_not_found"
                elif "email" in low_label:
                    answer = personal.get("email", "answer_not_found")
                elif "phone" in low_label:
                    answer = personal.get("phone", "answer_not_found")
                elif "linkedin" in low_label:
                    answer = personal.get("linkedin_url", "answer_not_found")
                elif "github" in low_label:
                    answer = personal.get("github_url", "answer_not_found")
                elif "website" in low_label:
                    answer = personal.get("website_url", "answer_not_found")
                elif "resume" in low_label or ftype == "input_file":
                     answer = str(config.RESUME_PDF_PATH)
                elif "visa" in low_label or "sponsor" in low_label:
                    answer = "No" if not work_auth.get("require_sponsorship") else "Yes"
                elif "authorized" in low_label:
                    answer = "Yes" if work_auth.get("legally_authorized_to_work") else "No"
                elif "worked at" in low_label and ("this company" in low_label or "previously" in low_label or "hiring company" in low_label):
                    answer = "No"
                elif "former" in low_label and "employee" in low_label:
                    answer = "No"
                elif "privacy" in low_label or "consent" in low_label or "policy" in low_label:
                    answer = "Yes"
                elif "onsite" in low_label or "on-site" in low_label or "hybrid" in low_label or "relocate" in low_label:
                    job_loc = (job.get("location") or "").lower()
                    if "nc" in job_loc or "north carolina" in job_loc:
                        answer = "Yes"
                    else:
                        answer = "No"
                elif "salary" in low_label:
                    answer = comp.get("salary_expectation", "answer_not_found")
                elif "based" in low_label or "location" in low_label or "country" in low_label or "reside" in low_label:
                    answer = personal.get("country") or personal.get("city", "") + ", " + personal.get("province_state", "")
                    if not answer or answer.strip() == ",":
                        answer = "United States"
                elif "pronoun" in low_label:
                    answer = "he/him"
                elif "hear about" in low_label or "referral" in low_label or "source" in low_label:
                    answer = "From the company jobs site"
                elif "gender" in low_label:

                    answer = eeo.get("gender", "answer_not_found")
                elif "race" in low_label:
                    answer = eeo.get("race_ethnicity", "answer_not_found")
                elif "veteran" in low_label:
                    answer = eeo.get("veteran_status", "answer_not_found")
                elif "disability" in low_label:
                    answer = eeo.get("disability_status", "answer_not_found")

            prepopulated.append({
                "id": fid,
                "label": label,
                "required": required,
                "type": ftype,
                "applicant_answer": answer
            })
            
    return prepopulated


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Submission Validation Helpers
# ---------------------------------------------------------------------------

def detect_validation_errors(page) -> list[str]:
    """Scrapes visible validation errors from the page (Greenhouse focus)."""
    return page.evaluate("""() => {
        let found = [];
        
        // 1. Greenhouse specific: elements with id ending in -error
        document.querySelectorAll('[id$="-error"]').forEach(el => {
            const text = el.innerText.trim();
            if (text) {
                const fieldId = el.id.replace('-error', '');
                const labelEl = document.querySelector(`label[for="${fieldId}"]`);
                const label = labelEl ? labelEl.innerText.trim() : 'Unknown Field';
                found.push(`Field "${label}" (ID: ${fieldId}) error: ${text}`);
            }
        });

        // 2. Greenhouse specific: li.error containers
        document.querySelectorAll('li.error').forEach(li => {
            const labelEl = li.querySelector('label');
            const label = labelEl ? labelEl.innerText.trim() : 'Unknown Field';
            const msgEl = li.querySelector('.error-message, .required-message, .required-error');
            const msg = msgEl ? msgEl.innerText.trim() : 'is required or invalid';
            const input = li.querySelector('input, select, textarea');
            found.push(`Field "${label}" (ID: ${input ? input.id : 'unknown'}) error: ${msg}`);
        });

        // 3. Generic invalid fields (browser level)
        document.querySelectorAll('input:invalid, select:invalid, textarea:invalid').forEach(el => {
            const labelEl = document.querySelector(`label[for="${el.id}"]`);
            const label = labelEl ? labelEl.innerText.trim() : 'Unknown Field';
            found.push(`Field "${label}" (ID: ${el.id}) is browser-marked as invalid or missing.`);
        });

        // 4. Aria-invalid fields
        document.querySelectorAll('[aria-invalid="true"]').forEach(el => {
            const labelEl = document.querySelector(`label[for="${el.id}"]`);
            const label = labelEl ? labelEl.innerText.trim() : 'Unknown Field';
            found.push(`Field "${label}" (ID: ${el.id}) is aria-marked as invalid.`);
        });

        return Array.from(new Set(found));
    }""")


def detect_success(page, initial_url: str | None = None) -> bool:
    """Checks if the page indicates a successful submission.
    
    Strictly focuses on Greenhouse patterns and URL redirects.
    """
    current_url = page.url.lower()
    initial_url = (initial_url or "").lower()
    
    # 1. URL Change to Confirmation (Strongest Signal)
    if "/confirmation" in current_url or "/thank-you" in current_url:
        if initial_url and initial_url in current_url and "/confirmation" not in initial_url:
            return True
        elif not initial_url:
            return True

    # 2. Specific Greenhouse Success Elements
    success_elements = [
        "#application_confirmation",
        ".thank-you-message",
        "h1:has-text('Application Submitted')",
        "h1:has-text('Thank you for applying')",
        ".confirmation-page"
    ]
    for sel in success_elements:
        try:
            if page.locator(sel).count() > 0:
                return True
        except:
            continue

    # 3. Disappearance of Form + Success Keywords
    # Only if the form was there and is now gone
    form_selectors = ["#application_form", "#main-form", ".job-application-form"]
    form_exists = False
    for sel in form_selectors:
        if page.locator(sel).count() > 0:
            form_exists = True
            break
            
    if not form_exists:
        success_indicators = ["application submitted", "received your application", "thank you for your application"]
        content = page.content().lower()
        if any(ind in content for ind in success_indicators):
            # Ensure it's not the generic "thank you for your interest"
            if "thank you for your interest" not in content or "submitted" in content:
                return True
            
    return False


# ---------------------------------------------------------------------------
# Per-job execution (Gemini Engine)
# ---------------------------------------------------------------------------

def run_job_gemini(job: dict, port: int, worker_id: int = 0,
                    model: str = config.DEFAULTS["model_gemini"], dry_run: bool = False) -> tuple[str, int, str | None, str | None]:
    """Execute a one-shot job application using Prepopulated-Schema + Gemini fallback."""
    start = time.time()
    
    # 0. Prepopulate & Check
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=start, actions=0, last_action="prepopulating")
    
    schema_str = job.get("application_schema")
    if not schema_str:
        add_event(f"[W{worker_id}] FAILED: No application_schema found.")
        return "failed:missing_application_schema", 0, None, None

    profile = config.load_profile()
    prepopulated_map = prepopulate_app(job, profile)
    if prepopulated_map:
        add_event(f"[W{worker_id}] Prepopulated local map ({len(prepopulated_map)} fields)")
        
        # IMMEDIATELY save initial mapping to database
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET application_prepopulated = ? WHERE url = ?",
            (json.dumps(prepopulated_map), job["url"])
        )
        conn.commit()

        # Activate LLM check
        prepopulated_map = check_prepopulated_app(job, profile, prepopulated_map, model=model)
        
        # Save verified prepopulated map to database (pre-apply state)
        conn.execute(
            "UPDATE jobs SET application_prepopulated = ? WHERE url = ?",
            (json.dumps(prepopulated_map), job["url"])
        )
        conn.commit()
        
        add_event(f"[W{worker_id}] LLM verified prepopulated map")
    else:
        add_event(f"[W{worker_id}] FAILED: Could not prepopulate map from schema.")
        return "failed:empty_application_schema", 0, None, None
    
    add_event(f"[W{worker_id}] Starting (Schema-Based): {job['title'][:40]} @ {job.get('site', '')}")

    url = job.get("application_url") or job["url"]
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            
            # 1. Navigate with robust wait
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as ge:
                add_event(f"[W{worker_id}] Warning: Initial navigation error: {str(ge)[:40]}")

            try:
                # Wait for universal form elements or a short timeout to ensure hydration
                page.wait_for_selector("button[type='submit'], input[type='submit'], input[type='file'], #first_name, #last_name, #email", timeout=20000)
            except:
                # Fallback: just wait a few seconds if selectors don't appear (might be a weird site)
                page.wait_for_timeout(5000)
            
            update_state(worker_id, last_action="Injecting prepopulated data", actions=1)
            
            # 2. Prepopulated Injection
            injection_script = """
            (mapStr) => {
                const prepopulated = JSON.parse(mapStr);
                let results = {};
                let failed = [];
                
                for (const item of prepopulated) {
                    const fid = item.id;
                    const value = item.applicant_answer;
                    const label = item.label;
                    
                    if (value === "answer_not_found" || value === "PDF_RESUME_UPLOAD" || value === "do_not_fill") {
                        failed.push(item);
                        continue;
                    }
                    
                    let el = document.getElementById(fid) || document.querySelector(`[name="${fid}"]`);
                    
                    // Fallback for labels
                    if (!el) {
                        const labels = Array.from(document.querySelectorAll('label'));
                        const targetLabel = labels.find(l => l.innerText.toLowerCase().includes(label.toLowerCase()));
                        if (targetLabel && targetLabel.htmlFor) {
                            el = document.getElementById(targetLabel.htmlFor);
                        }
                        if (!el && targetLabel) {
                            el = targetLabel.querySelector('input, select, textarea');
                        }
                    }

                    if (el) {
                        try {
                            if (el.tagName === 'SELECT') {
                                const options = Array.from(el.options);
                                const targetOpt = options.find(o => o.text.toLowerCase().includes(value.toLowerCase()) || o.value === value);
                                if (targetOpt) {
                                    el.value = targetOpt.value;
                                } else {
                                    el.value = value;
                                }
                            } else if (el.type === 'checkbox' || el.type === 'radio') {
                                if (value === true || value === "Yes" || value === "true" || value === "Checked") {
                                    el.checked = true;
                                } else {
                                    el.checked = false;
                                }
                            } else if (el.type !== 'file') {
                                el.value = value;
                            }
                            
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            results[fid] = value;
                        } catch (e) {
                            failed.push(item);
                        }
                    } else {
                        failed.push(item);
                    }
                }
                return { results, failed };
            }
            """
            
            inject_res = page.evaluate(injection_script, json.dumps(prepopulated_map))
            # Update map with results
            for item in prepopulated_map:
                if item["id"] in inject_res["results"]:
                    item["applicant_answer"] = inject_res["results"][item["id"]]
            
            failed_fields = inject_res["failed"]
            
            # 3. Resume Upload (Always)
            resume_path = config.RESUME_PDF_PATH
            if resume_path.exists():
                resume_selectors = ["input[type='file'][id='job_application_resume']", "input[type='file'][name*='resume']", "input[type='file']"]
                for sel in resume_selectors:
                    try:
                        input_el = page.locator(sel)
                        if input_el.count() > 0:
                            input_el.set_input_files(str(resume_path))
                            for item in prepopulated_map:
                                if "resume" in item["label"].lower() or item["type"] == "input_file":
                                    item["applicant_answer"] = str(resume_path)
                            break
                    except:
                        continue

            # 4. LLM Fallback with Complexity Check
            if failed_fields:
                update_state(worker_id, last_action="LLM surgical fallback", actions=2)
                
                # Scan for submit button details to give to LLM
                submit_info = page.evaluate("""() => {
                    const btn = document.querySelector("#submit_app") || document.querySelector("button[type='submit']") || document.querySelector("input[type='submit']");
                    if (btn) {
                        return {
                            id: btn.id || '',
                            text: btn.innerText || btn.value || '',
                            selector: btn.id ? `#${btn.id}` : (btn.tagName.toLowerCase() + (btn.type ? `[type='${btn.type}']` : ''))
                        };
                    }
                    return null;
                }""")
                
                # Build fallback structure
                fallback_structure = "\n".join([f"- {f['label']}{' (REQUIRED)' if f['required'] else ''} | ID: {f['id']} | Type: {f['type']}" for f in failed_fields])
                if submit_info:
                    fallback_structure += f"\n\nSUBMISSION BUTTON DETECTED: ID={submit_info['id']}, Text='{submit_info['text']}', Recommended Selector={submit_info['selector']}"
                else:
                    fallback_structure += "\n\nWARNING: NO SUBMISSION BUTTON DETECTED BY AUTOMATION. PLEASE SEARCH THE DOM FOR ONE."

                llm = get_client()
                
                # Complexity Check
                complexity_prompt = f"""You are an autonomous application agent. 
Analyze the following required fields that were NOT successfully populated by automation.
Determine if accurately populating these fields (potentially including finding them in the DOM and mapping answers) would take longer than 15 seconds.

FIELDS TO POPULATE:
{fallback_structure}

Criteria for "TOO_COMPLEX":
- More than 5 custom open-ended questions.
- Questions requiring research into company-specific facts not in profile.
- Extremely non-standard DOM structure (e.g. nested iframes, custom shadow DOMs).

Respond with ONLY "OK" or "TOO_COMPLEX".
"""
                complexity_resp = llm.ask(complexity_prompt, model=model).strip().upper()
                if "TOO_COMPLEX" in complexity_resp:
                    add_event(f"[W{worker_id}] ABORT: Form too complex (>15s estimate)")
                    final_data = {"questions": prepopulated_map, "llm_found_submit": "aborted_too_complex"}
                    return "failed:application too complex", int((time.time() - start) * 1000), json.dumps(final_data), json.dumps(prepopulated_map)

                # Surgical Fill
                prompt = prompt_mod.build_gemini_fill_prompt(job, fallback_structure)
                response = llm.ask(prompt, model=model)
                
                try:
                    json_str = response.strip()
                    if "```json" in json_str:
                        json_str = json_str.split("```json")[1].split("```")[0].strip()
                    elif "```" in json_str:
                        json_str = json_str.split("```")[1].split("```")[0].strip()
                    
                    fb_map = json.loads(json_str)
                    
                    # Check for mandatory first step: Submit Button
                    if fb_map.get("llm_found_submit") == "submit_not_found":
                        add_event(f"[W{worker_id}] ABORT: LLM could not find submit button.")
                        final_data = {"questions": prepopulated_map, "llm_found_submit": "submit_not_found"}
                        return "failed:no_submit_button", int((time.time() - start) * 1000), json.dumps(final_data), json.dumps(prepopulated_map)

                    for fid, data in fb_map.items():
                        if fid == "llm_found_submit": continue
                        if not fid or not isinstance(data, dict): continue
                        value = data.get("value")
                        
                        if value == "PDF_RESUME_UPLOAD": continue
                        
                        el = page.query_selector(f'[id="{fid}"]') or page.query_selector(f'[name="{fid}"]')
                        if el:
                            try:
                                el.scroll_into_view_if_needed()
                                tag = el.evaluate("e => e.tagName.toLowerCase()")
                                etype = el.evaluate("e => e.type")
                                if tag == "select":
                                    el.select_option(label=str(value))
                                elif etype in ("checkbox", "radio"):
                                    if value is True or str(value).lower() in ("true", "yes", "checked"):
                                        el.check(force=True)
                                    else:
                                        el.uncheck(force=True)
                                else:
                                    el.fill(str(value))
                                for item in prepopulated_map:
                                    if item["id"] == fid:
                                        item["applicant_answer"] = value
                                page.wait_for_timeout(100)
                            except:
                                pass
                except:
                    pass

            # 5. Submit & Verify Loop
            llm_found_submit = "submit_not_found"
            submit_btn = page.query_selector("#submit_app") or page.query_selector("button[type='submit']") or page.query_selector("input[type='submit']")
            
            if not submit_btn:
                # Last ditch effort: search for anything that looks like a submit button
                submit_btn = page.evaluate_handle("""() => {
                    const btns = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"]'));
                    return btns.find(b => {
                        const text = (b.innerText || b.value || '').toLowerCase();
                        return text.includes('submit') || text.includes('apply') || text.includes('finish');
                    });
                }""")
                if not submit_btn.as_element():
                    submit_btn = None

            if submit_btn:
                if dry_run:
                    llm_found_submit = "submit_found_not_pressed"
                    add_event(f"[W{worker_id}] DRY RUN: Found submit button.")
                else:
                    llm_found_submit = "submit_pressed"
                    add_event(f"[W{worker_id}] Clicking Submit...")
                    submit_btn.click()
                    
                    # 15-second verification loop
                    for attempt in range(15):
                        page.wait_for_timeout(1000)
                        
                        if detect_success(page, initial_url=url):
                            add_event(f"[W{worker_id}] APPLIED: Success confirmed!")
                            break
                        
                        errors = detect_validation_errors(page)
                        if errors:
                            add_event(f"[W{worker_id}] SUBMISSION FAILED: {len(errors)} validation errors found.")
                            
                            # CORRECTION COMPLEXITY CHECK
                            error_ctx = "\n".join([f"- {e}" for e in errors])
                            complexity_prompt = f"""You are an autonomous application agent. 
Analyze the following validation errors detected on a job application page after clicking submit.
Determine if accurately correcting these specific errors would take longer than 20 seconds.

ERRORS TO FIX:
{error_ctx}

Respond with ONLY "OK" or "TOO_COMPLEX".
"""
                            complexity_resp = llm.ask(complexity_prompt, model=model).strip().upper()
                            if "TOO_COMPLEX" in complexity_resp:
                                add_event(f"[W{worker_id}] ABORT: Correction too complex (>20s estimate)")
                                llm_found_submit = f"aborted_too_complex: {', '.join(errors)[:100]}"
                                break

                            # ATTEMPT FIX
                            update_state(worker_id, last_action="Correcting form errors", actions=3)
                            fix_prompt = prompt_mod.build_gemini_fix_errors_prompt(job, error_ctx)
                            fix_resp = llm.ask(fix_prompt, model=model)
                            
                            try:
                                json_str = fix_resp.strip()
                                if "```json" in json_str:
                                    json_str = json_str.split("```json")[1].split("```")[0].strip()
                                fix_map = json.loads(json_str)
                                
                                for fid, data in fix_map.items():
                                    val = data.get("value")
                                    # Try to find by ID or Name
                                    el = page.query_selector(f'[id="{fid}"]') or page.query_selector(f'[name="{fid}"]')
                                    if el:
                                        el.scroll_into_view_if_needed()
                                        el.fill(str(val))
                                        add_event(f"[W{worker_id}] Corrected field: {fid}")
                                
                                # Re-click Submit after fixing
                                add_event(f"[W{worker_id}] Retrying submission...")
                                submit_btn = page.query_selector("#submit_app") or page.query_selector("button[type='submit']") or page.query_selector("input[type='submit']")
                                if submit_btn:
                                    submit_btn.click()
                                    continue # Back to top of loop to wait again
                                else:
                                    add_event(f"[W{worker_id}] FAILED: Submit button lost after correction.")
                                    break
                            except Exception as fe:
                                logger.warning("Failed to parse or apply correction: %s", fe)
                                break 
                    
                    # Post-loop final check
                    if detect_success(page, initial_url=url):
                        llm_found_submit = "submit_pressed_success"
                    else:
                        errors = detect_validation_errors(page)
                        if errors:
                            llm_found_submit = f"failed_validation: {', '.join(errors)[:100]}"
                        else:
                            # Check if the submit button is still visible and enabled
                            is_visible = page.evaluate("(btn) => btn && btn.offsetParent !== null && !btn.disabled", submit_btn)
                            if is_visible:
                                llm_found_submit = "failed_submission_stuck: Button still visible/enabled after click"
                            else:
                                llm_found_submit = "failed_timeout_or_no_response"

            # 6. Final State Capture & Database Update
            duration_ms = int((time.time() - start) * 1000)
            final_data = {
                "questions": prepopulated_map,
                "llm_found_submit": llm_found_submit,
                "final_url": page.url,
                "browser_context": {
                    "url": page.url,
                    "title": page.title(),
                    "errors_detected": detect_validation_errors(page)
                }
            }
            details_json = json.dumps(final_data)

            # Update application_schema with audit log
            conn = get_connection()
            conn.execute("UPDATE jobs SET application_schema = ? WHERE url = ?", (details_json, job["url"]))
            conn.commit()

            if llm_found_submit == "submit_pressed_success":
                update_state(worker_id, status="applied", last_action="Applied!")
                return "applied", duration_ms, details_json, json.dumps(prepopulated_map)
            elif dry_run and llm_found_submit == "submit_found_not_pressed":
                update_state(worker_id, status="applied", last_action="DRY RUN OK")
                return "applied", duration_ms, details_json, json.dumps(prepopulated_map)
            else:
                reason = llm_found_submit if ":" in llm_found_submit else f"failed:{llm_found_submit}"
                add_event(f"[W{worker_id}] FAILED: {reason}")
                return reason, duration_ms, details_json, json.dumps(prepopulated_map)

        except Exception as e:
            logger.exception("Apply error")
            duration_ms = int((time.time() - start) * 1000)
            final_data = {"questions": prepopulated_map, "llm_found_submit": "error_occurred", "error": str(e)}
            return f"failed:{str(e)[:100]}", duration_ms, json.dumps(final_data), json.dumps(prepopulated_map)
        finally:
            pass


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = config.DEFAULTS["model_gemini"], dry_run: bool = False) -> tuple[str, int, str | None, str | None]:
    """Execute a job application session."""
    return run_job_gemini(job, port, worker_id, model=model, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = config.DEFAULTS["model_gemini"], dry_run: bool = False,
                custom_sql: str | None = None,
                continuous: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = all currently eligible).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Gemini model name.
        dry_run: Don't click Submit.
        custom_sql: Custom SQL query for job selection.
        continuous: Run forever, polling for new jobs.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and limit > 0 and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, custom_sql=custom_sql)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms, details_json, prepop_json = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)
            
            # Update cumulative cost from LLM client
            llm_client = get_client()
            update_state(worker_id, total_cost=llm_client.total_cost)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms, 
                           application_details=details_json,
                           application_prepopulated=prepop_json)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            duration_ms=duration_ms,
                            application_details=details_json,
                            application_prepopulated=prepop_json)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = True, model: str = config.DEFAULTS["model_gemini"],
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1, custom: bool = False,
         custom_sql: str | None = None) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Gemini model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        custom: Use custom SQL query from test/custom_records.sql.
        custom_sql: Pre-loaded custom SQL string.
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    # Load and validate custom SQL if not provided
    if custom and not custom_sql:
        from applypilot.database import validate_and_load_custom_sql
        custom_sql = validate_and_load_custom_sql()

    if custom_sql:
        # Check how many jobs in the custom subset are actually ready to apply
        conn = get_connection()
        rows = conn.execute(f"WITH subset AS ({custom_sql}) SELECT * FROM subset WHERE fit_score >= ?", (min_score,)).fetchall()
        
        ready_count = sum(1 for r in rows if r["application_url"])
        missing_url = sum(1 for r in rows if not r["application_url"])
        
        if ready_count == 0 and missing_url > 0:
            console.print(f"[yellow]Warning: {missing_url} jobs found with score >= {min_score}, but NONE have an application_url.[/yellow]")
            console.print("[dim]Run 'applypilot run enrich --custom' first to retrieve apply URLs.[/dim]")
        elif missing_url > 0:
            console.print(f"[dim]Note: {missing_url} jobs in subset lack an application_url and will be skipped.[/dim]")

        if ready_count > 0:
            console.print(f"[green]Custom SQL validated ({ready_count} jobs ready to apply).[/green]")

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # No-op here as Gemini Code is removed
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    custom_sql=custom_sql,
                    continuous=continuous,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            custom_sql=custom_sql,
                            continuous=continuous,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
