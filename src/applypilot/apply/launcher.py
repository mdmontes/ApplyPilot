"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
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
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
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
    """Generate a prompt file and print the Claude CLI command for manual debugging.

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

def extract_form_structure(page) -> str:
    """Extract visible form fields and their associated labels/IDs."""
    fields = page.evaluate("""() => {
        const results = [];
        const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
        
        inputs.forEach(input => {
            // Skip hidden or non-functional inputs
            const style = window.getComputedStyle(input);
            if (input.type === 'hidden' || style.display === 'none' || style.visibility === 'hidden') return;
            if (input.type === 'submit' || input.type === 'button') return;

            let labelText = '';
            // 1. Label by 'for'
            if (input.id) {
                const label = document.querySelector(`label[for="${input.id}"]`);
                if (label) labelText = label.innerText;
            }
            // 2. Parent label
            if (!labelText) {
                const parentLabel = input.closest('label');
                if (parentLabel) labelText = parentLabel.innerText;
            }
            // 3. Aria-label or Placeholder
            if (!labelText) labelText = input.getAttribute('aria-label') || input.placeholder || '';

            results.push({
                id: input.id || '',
                name: input.name || '',
                type: input.tagName.toLowerCase() === 'input' ? input.type : input.tagName.toLowerCase(),
                label: labelText.replace(/\\n/g, ' ').trim(),
                required: input.required || input.getAttribute('aria-required') === 'true'
            });
        });
        return results;
    }""")
    
    lines = []
    for f in fields:
        req = " (REQUIRED)" if f['required'] else ""
        lines.append(f"- {f['label']}{req} | ID: {f['id']} | Type: {f['type']}")
    
    return "\n".join(lines)


def run_job_gemini(job: dict, port: int, worker_id: int = 0,
                    model: str = "gemini-2.0-flash", dry_run: bool = False) -> tuple[str, int]:
    """Execute a one-shot job application using Gemini + Playwright."""
    start = time.time()
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=start, actions=0, last_action="navigating")
    add_event(f"[W{worker_id}] Starting (Gemini): {job['title'][:40]} @ {job.get('site', '')}")

    url = job.get("application_url") or job["url"]
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            
            # 1. Navigate
            page.goto(url, wait_until="networkidle", timeout=60000)
            update_state(worker_id, last_action="extracting form", actions=1)
            
            # 2. Extract Structure
            form_text = extract_form_structure(page)
            
            # 3. Ask Gemini
            prompt = prompt_mod.build_gemini_fill_prompt(job, form_text)
            llm = get_client()
            response = llm.ask(prompt)
            
            # Clean up JSON response (it might have markdown blocks)
            json_str = response.strip()
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
            
            field_map = json.loads(json_str)
            
            # 4. Fill Form
            update_state(worker_id, last_action="filling form", actions=2)
            for fid, value in field_map.items():
                if not fid: continue
                
                if value == "PDF_RESUME_UPLOAD":
                    # Handle resume upload
                    if config.RESUME_PDF_PATH.exists():
                        try:
                            page.set_input_files(f"#{fid}", str(config.RESUME_PDF_PATH))
                        except Exception as e:
                            add_event(f"[W{worker_id}] Upload error on {fid}: {str(e)[:40]}")
                    continue
                
                # Try to fill based on type
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
                        is_checked = value is True or str(value).lower() in ("true", "yes", "checked")
                        if is_checked:
                            el.check(force=True)
                        else:
                            el.uncheck(force=True)
                    elif etype == "radio":
                        el.check(force=True)
                    else:
                        # For Greenhouse 'remix' inputs, sometimes fill() fails if hidden.
                        # We try fill() first, then fallback to type()
                        try:
                            el.fill(str(value), timeout=2000)
                        except:
                            el.focus()
                            page.keyboard.type(str(value), delay=20)
                    
                    page.wait_for_timeout(200) # Small breather between fields
                except Exception as fe:
                    add_event(f"[W{worker_id}] Warning: Could not fill {fid}: {str(fe)[:40]}")
            
            # 5. Submit
            duration_ms = int((time.time() - start) * 1000)
            if dry_run:
                add_event(f"[W{worker_id}] DRY RUN: Form filled, skipping submit.")
                update_state(worker_id, status="applied", last_action="DRY RUN OK")
                return "applied", duration_ms
            
            update_state(worker_id, last_action="submitting", actions=3)
            # Find Greenhouse submit button (usually id="submit_app" or similar)
            submit_btn = page.query_selector("#submit_app") or page.query_selector("button[type='submit']")
            if submit_btn:
                submit_btn.click()
                page.wait_for_timeout(5000) # Wait for navigation or message
                
                # Check for success
                success_indicators = ["thank you", "received", "submitted", "application confirmation"]
                content = page.content().lower()
                if any(ind in content for ind in success_indicators):
                    add_event(f"[W{worker_id}] APPLIED: Success indicator found.")
                    update_state(worker_id, status="applied", last_action="Applied!")
                    return "applied", duration_ms
                else:
                    add_event(f"[W{worker_id}] Warning: No clear success message found.")
                    return "applied", duration_ms # Assume success if no error shown
            else:
                add_event(f"[W{worker_id}] FAILED: Could not find submit button.")
                return "failed:no_submit_button", duration_ms

        except Exception as e:
            logger.exception("Gemini apply error")
            duration_ms = int((time.time() - start) * 1000)
            return f"failed:{str(e)[:100]}", duration_ms
        finally:
            # We don't close the browser here, cleanup_worker does it
            pass


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "gemini-2.0-flash", dry_run: bool = False) -> tuple[str, int]:
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
        model: Claude model name.
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

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
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
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1, custom: bool = False) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        custom: Use custom SQL query from test/custom_records.sql.
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    # Load and validate custom SQL
    custom_sql = None
    if custom:
        from applypilot.database import validate_and_load_custom_sql
        custom_sql = validate_and_load_custom_sql()

        # Additional check for apply stage: application_url presence
        conn = get_connection()
        sample_rows = conn.execute(custom_sql).fetchall()
        for row in sample_rows:
            if not row["application_url"]:
                console.print(f"[red]Error: Custom SQL returned rows with empty application_url.[/red]")
                console.print(f"Row URL: {row['url']}")
                sys.exit(1)

        console.print(f"[green]Custom SQL validated ({len(sample_rows)} jobs in pool).[/green]")

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
            # No-op here as Claude Code is removed
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
