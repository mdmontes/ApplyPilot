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
                       fit_score, location, full_description
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
                           fit_score, location, full_description
                    FROM custom_subset
                    WHERE (apply_status IS NULL OR apply_status = 'failed')
                      AND (apply_attempts IS NULL OR apply_attempts < ?)
                      AND fit_score >= ?
                      AND application_url IS NOT NULL
                    ORDER BY fit_score DESC, url
                    LIMIT 1
                """
            else:
                query = """
                    SELECT url, title, site, application_url,
                           fit_score, location, full_description
                    FROM jobs
                    WHERE (apply_status IS NULL OR apply_status = 'failed')
                      AND (apply_attempts IS NULL OR apply_attempts < ?)
                      AND fit_score >= ?
                    ORDER BY fit_score DESC, url
                    LIMIT 1
                """
            row = conn.execute(query, [config.DEFAULTS["max_apply_attempts"], min_score]).fetchone()

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
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None,
                application_details: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           application_details = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, application_details, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           application_details = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, application_details, url))
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
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
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
# Per-job execution (Gemini Engine)
# ---------------------------------------------------------------------------

def run_job_gemini(job: dict, port: int, worker_id: int = 0,
                    model: str = "gemini-2.0-flash", dry_run: bool = False) -> tuple[str, int, str | None]:
    """Execute a one-shot job application using Partial-DOM + Gemini fallback."""
    start = time.time()
    application_details = {}
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=start, actions=0, last_action="navigating")
    add_event(f"[W{worker_id}] Starting (Partial-DOM): {job['title'][:40]} @ {job.get('site', '')}")

    url = job.get("application_url") or job["url"]
    profile = config.load_profile()
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            
            # 1. Navigate
            page.goto(url, wait_until="networkidle", timeout=60000)
            update_state(worker_id, last_action="Identifying form context", actions=1)
            
            # 2. Robust Programmatic Injection
            static_field_map = {
                "first_name": profile["personal"].get("full_name", "").split()[0],
                "last_name": profile["personal"].get("full_name", "").split()[-1] if " " in profile["personal"].get("full_name", "") else "",
                "email": profile["personal"].get("email", ""),
                "phone": profile["personal"].get("phone", ""),
                "org": profile["experience"].get("current_company", profile["experience"].get("current_title", "")),
                "linkedin": profile["personal"].get("linkedin_url", ""),
                "github": profile["personal"].get("github_url", ""),
                "website": profile["personal"].get("website_url", ""),
                "portfolio": profile["personal"].get("portfolio_url", "")
            }

            injection_script = """
            (profileStr) => {
                const data = JSON.parse(profileStr);
                const mappings = [
                    { key: 'first_name', label: 'First Name', selectors: ['#first_name', 'input[name="first_name"]', 'input[autocomplete="given-name"]'] },
                    { key: 'last_name', label: 'Last Name', selectors: ['#last_name', 'input[name="last_name"]', 'input[autocomplete="family-name"]'] },
                    { key: 'email', label: 'Email', selectors: ['#email', 'input[name="email"]', 'input[type="email"]', 'input[autocomplete="email"]'] },
                    { key: 'phone', label: 'Phone', selectors: ['#phone', 'input[name="phone"]', 'input[type="tel"]', 'input[autocomplete="tel"]'] },
                    { key: 'org', label: 'Company', selectors: ['#org', 'input[name="org"]', 'input[name="company"]'] },
                    { key: 'linkedin', label: 'LinkedIn', selectors: ['#urls\\\\[LinkedIn\\\\]', 'input[name*="linkedin"]'] },
                    { key: 'github', label: 'GitHub', selectors: ['#urls\\\\[GitHub\\\\]', 'input[name*="github"]'] },
                    { key: 'website', label: 'Website', selectors: ['#urls\\\\[Website\\\\]', 'input[name*="website"]'] },
                    { key: 'portfolio', label: 'Portfolio', selectors: ['#urls\\\\[Portfolio\\\\]', 'input[name*="portfolio"]'] }
                ];
                
                let results = {};
                for (const m of mappings) {
                    const value = data[m.key];
                    if (!value) continue;
                    
                    let el = null;
                    for (const s of m.selectors) {
                        el = document.querySelector(s);
                        if (el) break;
                    }
                    
                    // Fallback: search by label text
                    if (!el) {
                        const labels = Array.from(document.querySelectorAll('label'));
                        const targetLabel = labels.find(l => l.innerText.toLowerCase().includes(m.label.toLowerCase()));
                        if (targetLabel && targetLabel.htmlFor) {
                            el = document.getElementById(targetLabel.htmlFor);
                        }
                        if (!el && targetLabel) {
                            el = targetLabel.querySelector('input, select, textarea');
                        }
                    }

                    if (el) {
                        el.value = value;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        results[m.label] = value;
                    }
                }
                return results;
            }
            """
            
            programmatic_filled = page.evaluate(injection_script, json.dumps(static_field_map))
            application_details.update(programmatic_filled)
            
            # 3. Initial Programmatic Resume Upload
            resume_path = config.RESUME_PDF_PATH
            resume_uploaded = False
            if resume_path.exists():
                # Try common Greenhouse ID and generic selectors
                resume_selectors = ["input[type='file'][id='job_application_resume']", "input[type='file'][name*='resume']"]
                for sel in resume_selectors:
                    input_el = page.locator(sel)
                    if input_el.count() > 0:
                        input_el.set_input_files(str(resume_path))
                        application_details["Resume/CV"] = str(resume_path)
                        resume_uploaded = True
                        break
            
            # 4. LLM Fallback with Cost Cap
            update_state(worker_id, last_action="evaluating missing fields", actions=2)
            
            # Capture all unfilled fields (not just required, to maximize quality)
            unfilled_fields = page.evaluate("""() => {
                const results = [];
                const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
                inputs.forEach(input => {
                    const style = window.getComputedStyle(input);
                    if (input.type === 'hidden' || style.display === 'none' || style.visibility === 'hidden') return;
                    if (input.type === 'submit' || input.type === 'button') return;
                    
                    // Only process if currently empty
                    if (!input.value || (input.type === 'file' && input.files.length === 0)) {
                        let labelText = '';
                        if (input.id) {
                            const label = document.querySelector(`label[for="${input.id}"]`);
                            if (label) labelText = label.innerText;
                        }
                        if (!labelText) {
                            const parentLabel = input.closest('label');
                            if (parentLabel) labelText = parentLabel.innerText;
                        }
                        if (!labelText) labelText = input.getAttribute('aria-label') || input.placeholder || '';

                        const isRequired = input.required || input.getAttribute('aria-required') === 'true' || 
                                         input.closest('.required') || input.parentElement.innerText.includes('*');
                        
                        const isResumeField = input.type === 'file' || 
                                           labelText.toLowerCase().includes('resume') || 
                                           labelText.toLowerCase().includes('cv');

                        // ALWAYS include resume/file fields, even if not required
                        if (isRequired || isResumeField) {
                            results.push({
                                id: input.id || '',
                                name: input.name || '',
                                type: input.tagName.toLowerCase() === 'input' ? input.type : input.tagName.toLowerCase(),
                                label: labelText.replace(/\\n/g, ' ').trim(),
                                required: isRequired
                            });
                        }
                    }
                });
                return results;
            }""")
            
            if unfilled_fields:
                # Build fallback structure
                fallback_structure = "\n".join([f"- {f['label']}{' (REQUIRED)' if f['required'] else ''} | ID: {f['id']} | Type: {f['type']}" for f in unfilled_fields])
                prompt = prompt_mod.build_gemini_fill_prompt(job, fallback_structure)
                
                # Cost Cap Enforcement
                estimated_tokens = len(prompt) / 4
                estimated_cost = (estimated_tokens / 1_000_000) * 0.10
                
                if estimated_cost > 0.05:
                    add_event(f"[W{worker_id}] ABORT: Estimated cost ${estimated_cost:.4f} > $0.05 cap.")
                    return "failed:application too expensive", int((time.time() - start) * 1000), json.dumps(application_details)

                update_state(worker_id, last_action="Gemini fallback", actions=3)
                llm = get_client()
                response = llm.ask(prompt)
                
                # Parse JSON
                try:
                    json_str = response.strip()
                    if "```json" in json_str:
                        json_str = json_str.split("```json")[1].split("```")[0].strip()
                    elif "```" in json_str:
                        json_str = json_str.split("```")[1].split("```")[0].strip()
                    
                    fb_map = json.loads(json_str)
                    
                    # Fill fallback fields
                    for fid, data in fb_map.items():
                        if not fid or not isinstance(data, dict): continue
                        
                        value = data.get("value")
                        label = data.get("label", fid)
                        
                        if value == "PDF_RESUME_UPLOAD":
                            if resume_path.exists():
                                try:
                                    page.locator(f'[id="{fid}"]').set_input_files(str(resume_path))
                                    application_details[label] = str(resume_path)
                                except Exception as e:
                                    add_event(f"[W{worker_id}] LLM Resume Upload Error: {str(e)[:40]}")
                            continue

                        selector = f'[id="{fid}"]'
                        el = page.query_selector(selector)
                        if not el: continue
                        
                        try:
                            el.scroll_into_view_if_needed()
                            tag = el.evaluate("e => e.tagName.toLowerCase()")
                            etype = el.evaluate("e => e.type")
                            
                            if tag == "select":
                                el.select_option(label=str(value))
                            elif etype == "checkbox":
                                if value is True or str(value).lower() in ("true", "yes", "checked"):
                                    el.check(force=True)
                                else:
                                    el.uncheck(force=True)
                            elif etype == "radio":
                                el.check(force=True)
                            else:
                                el.fill(str(value))
                            
                            application_details[label] = value
                            page.wait_for_timeout(100)
                        except Exception as fe:
                            add_event(f"[W{worker_id}] Warning: Could not fill {fid}: {str(fe)[:40]}")
                except Exception as pe:
                    add_event(f"[W{worker_id}] Gemini response parse error: {str(pe)[:40]}")

            # 5. Submit
            duration_ms = int((time.time() - start) * 1000)
            details_json = json.dumps(application_details)

            # Safeguard: Do not submit (or report success) if no fields were populated
            if len(application_details) == 0:
                add_event(f"[W{worker_id}] FAILED: 0 fields populated (possible captcha or unseen blocking element).")
                return "failed:no_fields_populated", duration_ms, details_json
            
            if dry_run:
                add_event(f"[W{worker_id}] DRY RUN: {len(application_details)} fields populated.")
                update_state(worker_id, status="applied", last_action="DRY RUN OK")
                return "applied", duration_ms, details_json
            
            update_state(worker_id, last_action="submitting", actions=4)
            submit_btn = page.query_selector("#submit_app") or page.query_selector("button[type='submit']")
            if submit_btn:
                submit_btn.click()
                page.wait_for_timeout(5000)
                
                success_indicators = ["thank you", "received", "submitted", "application confirmation"]
                content = page.content().lower()
                if any(ind in content for ind in success_indicators):
                    add_event(f"[W{worker_id}] APPLIED: Success indicator found.")
                    update_state(worker_id, status="applied", last_action="Applied!")
                    return "applied", duration_ms, details_json
                else:
                    add_event(f"[W{worker_id}] Warning: No clear success message found.")
                    return "applied", duration_ms, details_json
            else:
                add_event(f"[W{worker_id}] FAILED: Could not find submit button.")
                return "failed:no_submit_button", duration_ms, details_json

        except Exception as e:
            logger.exception("Apply error")
            duration_ms = int((time.time() - start) * 1000)
            return f"failed:{str(e)[:100]}", duration_ms, json.dumps(application_details)
        finally:
            pass


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "gemini-2.0-flash", dry_run: bool = False) -> tuple[str, int, str | None]:
    """Execute a job application session."""
    return run_job_gemini(job, port, worker_id, model=model, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
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

            result, duration_ms, details_json = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)
            
            # Update cumulative cost from LLM client
            llm_client = get_client()
            update_state(worker_id, total_cost=llm_client.total_cost)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms, application_details=details_json)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms,
                            application_details=details_json)
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
         min_score: int = 7, headless: bool = True, model: str = "sonnet",
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
