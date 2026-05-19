"""ApplyPilot Pipeline Orchestrator.

Runs pipeline stages in sequence or concurrently (streaming mode).

Usage (via CLI):
    applypilot run                        # all stages, sequential
    applypilot run --stream               # all stages, concurrent
    applypilot run discover enrich        # specific stages
    applypilot run score                  # LLM scoring stage
    applypilot run --dry-run              # preview without executing
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from applypilot.config import load_env, ensure_dirs
from applypilot.database import init_db, get_connection, get_stats

log = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------

STAGE_ORDER = ("discover", "enrich", "score", "apply")

STAGE_META: dict[str, dict] = {
    "discover": {"desc": "Job discovery (JobSpy + Workday + smart extract)"},
    "enrich":   {"desc": "Detail enrichment (full descriptions + apply URLs)"},
    "score":    {"desc": "LLM scoring (fit 1-10)"},
    "apply":    {"desc": "Auto-apply (autonomous browser submission)"},
}

# Upstream dependency: a stage only finishes when its upstream is done AND
# it has no remaining pending work.
_UPSTREAM: dict[str, str | None] = {
    "discover": None,
    "enrich":   "discover",
    "score":    "enrich",
    "apply":    "score",
}


# ---------------------------------------------------------------------------
# Individual stage runners
# ---------------------------------------------------------------------------

def _run_discover(workers: int = 1, dry_run: bool = False) -> dict:
    """Stage: Job discovery — JobSpy, Workday, and smart-extract scrapers."""
    if dry_run:
        console.print("  [yellow]Dry-run: skipping discovery[/yellow]")
        return {"status": "ok"}
    stats: dict = {"jobspy": None, "workday": None, "smartextract": None}

    # JobSpy
    console.print("  [cyan]JobSpy full crawl...[/cyan]")
    try:
        from applypilot.discovery.jobspy import run_discovery
        run_discovery()
        stats["jobspy"] = "ok"
    except Exception as e:
        log.error("JobSpy crawl failed: %s", e)
        console.print(f"  [red]JobSpy error:[/red] {e}")
        stats["jobspy"] = f"error: {e}"

    # Workday corporate scraper
    console.print("  [cyan]Workday corporate scraper...[/cyan]")
    try:
        from applypilot.discovery.workday import run_workday_discovery
        run_workday_discovery(workers=workers)
        stats["workday"] = "ok"
    except Exception as e:
        log.error("Workday scraper failed: %s", e)
        console.print(f"  [red]Workday error:[/red] {e}")
        stats["workday"] = f"error: {e}"

    # Smart extract
    console.print("  [cyan]Smart extract (AI-powered scraping)...[/cyan]")
    try:
        from applypilot.discovery.smartextract import run_smart_extract
        run_smart_extract(workers=workers)
        stats["smartextract"] = "ok"
    except Exception as e:
        log.error("Smart extract failed: %s", e)
        console.print(f"  [red]Smart extract error:[/red] {e}")
        stats["smartextract"] = f"error: {e}"

    return stats


def _run_enrich(workers: int = 1, dry_run: bool = False) -> dict:
    """Stage: Detail enrichment — scrape full descriptions and apply URLs."""
    if dry_run:
        console.print("  [yellow]Dry-run: skipping enrichment[/yellow]")
        return {"status": "ok"}
    try:
        from applypilot.enrichment.detail import run_enrichment
        run_enrichment(workers=workers)
        return {"status": "ok"}
    except Exception as e:
        log.error("Enrichment failed: %s", e)
        return {"status": f"error: {e}"}


def _run_score(dry_run: bool = False) -> dict:
    """Stage: LLM scoring — assign fit scores 1-10."""
    if dry_run:
        console.print("  [yellow]Dry-run: skipping scoring[/yellow]")
        return {"status": "ok"}
    try:
        from applypilot.scoring.scorer import run_scoring
        run_scoring()
        return {"status": "ok"}
    except Exception as e:
        log.error("Scoring failed: %s", e)
        return {"status": f"error: {e}"}


def _run_apply(min_score: int = 7, workers: int = 1, dry_run: bool = False) -> dict:
    """Stage: Auto-apply — autonomous browser submission."""
    try:
        from applypilot.apply.launcher import main as apply_main
        apply_main(
            limit=0,  # Process all eligible jobs
            min_score=min_score,
            workers=workers,
            dry_run=dry_run,
        )
        return {"status": "ok"}
    except Exception as e:
        log.error("Auto-apply failed: %s", e)
        return {"status": f"error: {e}"}


# Map stage names to their runner functions
_STAGE_RUNNERS: dict[str, callable] = {
    "discover": _run_discover,
    "enrich":   _run_enrich,
    "score":    _run_score,
    "apply":    _run_apply,
}


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------

def _resolve_stages(input_stages: list[str]) -> list[str]:
    """Given a list of requested stages, return them in STAGE_ORDER."""
    if "all" in input_stages:
        return list(STAGE_ORDER)

    # Filter and sort
    resolved = [s for s in STAGE_ORDER if s in input_stages]
    return resolved


# ---------------------------------------------------------------------------
# Streaming mode logic
# ---------------------------------------------------------------------------

class _StageTracker:
    """Thread-safe tracker for stage completion in streaming mode."""
    def __init__(self):
        self._done: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def mark_done(self, stage: str, result: dict):
        with self._lock:
            self._done[stage] = result
            self._cv.notify_all()

    def is_done(self, stage: str) -> bool:
        with self._lock:
            return stage in self._done

    def wait(self, stage: str, timeout: float | None = None):
        with self._lock:
            if stage in self._done:
                return True
            return self._cv.wait(timeout)

    def get_result(self, stage: str) -> dict | None:
        with self._lock:
            return self._done.get(stage)


# How long to sleep between polling loops in streaming mode (seconds)
_STREAM_POLL_INTERVAL = 10


# Stage -> SQL to count items needing this stage
_PENDING_SQL: dict[str, str] = {
    "enrich": "SELECT COUNT(*) FROM jobs WHERE full_description IS NULL",
    "score":  "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL",
    "apply":  "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? AND applied_at IS NULL AND application_url IS NOT NULL",
}


def _count_pending(stage: str, min_score: int = 7) -> int:
    """Count pending work items for a stage."""
    sql = _PENDING_SQL.get(stage)
    if sql is None:
        return 0
    conn = get_connection()
    if "?" in sql:
        return conn.execute(sql, (min_score,)).fetchone()[0]
    return conn.execute(sql).fetchone()[0]


def _run_stage_streaming(
    stage: str,
    tracker: _StageTracker,
    stop_event: threading.Event,
    min_score: int = 7,
    workers: int = 1,
    dry_run: bool = False,
) -> None:
    """Run a single stage in streaming mode: loop until upstream done + no work.

    For discover: runs once, then marks done.
    For all others: polls DB for pending work, runs the batch processor,
    and repeats until upstream is done and no pending work remains.
    """
    runner = _STAGE_RUNNERS[stage]
    kwargs: dict = {"dry_run": dry_run}
    if stage in ("discover", "enrich", "apply"):
        kwargs["workers"] = workers
    if stage == "apply":
        kwargs["min_score"] = min_score

    upstream = _UPSTREAM[stage]

    if stage == "discover":
        # Discover runs once (its sub-scrapers already do their full crawl)
        try:
            result = runner(**kwargs)
            tracker.mark_done(stage, result)
        except Exception as e:
            log.exception("Stage '%s' crashed", stage)
            tracker.mark_done(stage, {"status": f"error: {e}"})
        return

    # For downstream stages: loop until upstream done + no pending work
    passes = 0
    while not stop_event.is_set():
        # Wait for upstream to start producing work (first pass only)
        if passes == 0 and upstream and not tracker.is_done(upstream):
            # Wait a bit for upstream to produce some work before first run
            tracker.wait(upstream, timeout=_STREAM_POLL_INTERVAL)

        pending = _count_pending(stage, min_score)

        if pending > 0:
            try:
                runner(**kwargs)
                passes += 1
            except Exception as e:
                log.error("Stage '%s' error (pass %d): %s", stage, passes, e)
                passes += 1
        else:
            # No work right now
            upstream_done = upstream is None or tracker.is_done(upstream)
            if upstream_done:
                # No work and upstream is done — this stage is finished
                break
            # Upstream still running, wait and retry
            if stop_event.wait(timeout=_STREAM_POLL_INTERVAL):
                break  # Stop requested

    tracker.mark_done(stage, {"status": "ok", "passes": passes})


# ---------------------------------------------------------------------------
# Pipeline orchestrators
# ---------------------------------------------------------------------------

def _run_sequential(ordered: list[str], min_score: int, workers: int = 1, dry_run: bool = False) -> dict:
    """Execute stages one at a time (original behavior)."""
    results: list[dict] = []
    errors: dict[str, str] = {}
    pipeline_start = time.time()

    for name in ordered:
        meta = STAGE_META[name]
        console.print(f"\n{'=' * 70}")
        console.print(f"  [bold]STAGE: {name}[/bold] — {meta['desc']}")
        console.print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
        console.print(f"{'=' * 70}")

        t0 = time.time()
        runner = _STAGE_RUNNERS[name]

        try:
            kwargs: dict = {"dry_run": dry_run}
            if name in ("discover", "enrich", "apply"):
                kwargs["workers"] = workers
            if name == "apply":
                kwargs["min_score"] = min_score
            result = runner(**kwargs)
            elapsed = time.time() - t0

            status = "ok"
            if isinstance(result, dict):
                status = result.get("status", "ok")
                if name == "discover":
                    sub_errors = [
                        f"{k}: {v}" for k, v in result.items()
                        if isinstance(v, str) and v.startswith("error")
                    ]
                    if sub_errors:
                        status = "partial"

        except Exception as e:
            elapsed = time.time() - t0
            status = f"error: {e}"
            log.exception("Stage '%s' crashed", name)
            console.print(f"\n  [red]STAGE FAILED:[/red] {e}")

        results.append({"stage": name, "status": status, "elapsed": elapsed})
        if status not in ("ok", "partial"):
            errors[name] = status

        console.print(f"\n  Stage '{name}' completed in {elapsed:.1f}s — {status}")

    total_elapsed = time.time() - pipeline_start
    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


def _run_streaming(ordered: list[str], min_score: int, workers: int = 1, dry_run: bool = False) -> dict:
    """Execute stages concurrently with DB as conveyor belt."""
    tracker = _StageTracker()
    stop_event = threading.Event()
    pipeline_start = time.time()

    console.print(f"\n  [bold cyan]STREAMING MODE[/bold cyan] — stages run concurrently")
    console.print(f"  Poll interval: {_STREAM_POLL_INTERVAL}s\n")

    # Mark stages NOT in `ordered` as done so downstream doesn't wait for them
    for stage in STAGE_ORDER:
        if stage not in ordered:
            tracker.mark_done(stage, {"status": "skipped"})

    # Launch each stage in its own thread
    threads: dict[str, threading.Thread] = {}
    start_times: dict[str, float] = {}

    for name in ordered:
        start_times[name] = time.time()
        t = threading.Thread(
            target=_run_stage_streaming,
            kwargs={
                "stage": name,
                "tracker": tracker,
                "stop_event": stop_event,
                "min_score": min_score,
                "workers": workers,
                "dry_run": dry_run,
            },
            name=f"pipeline-{name}"
        )
        t.start()
        threads[name] = t

    # Wait for all to finish
    try:
        for name, t in threads.items():
            t.join()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping pipeline...[/yellow]")
        stop_event.set()
        for t in threads.values():
            t.join()

    # Collect results
    results = []
    errors = {}
    for name in ordered:
        res = tracker.get_result(name) or {"status": "error: missing result"}
        results.append({
            "stage": name,
            "status": res["status"],
            "elapsed": time.time() - start_times[name]
        })
        if res["status"] not in ("ok", "partial"):
            errors[name] = res["status"]

    total_elapsed = time.time() - pipeline_start
    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    stages: list[str],
    min_score: int = 7,
    dry_run: bool = False,
    stream: bool = False,
    workers: int = 1,
) -> dict:
    """Run specified pipeline stages in order.

    - sequential: runs one stage to completion, then next
    - stream: runs all stages concurrently using DB as conveyor belt
    """
    ordered = _resolve_stages(stages)
    if not ordered:
        return {"stages": [], "errors": {}, "elapsed": 0}

    ensure_dirs()
    load_env()

    # Execute
    if stream:
        result = _run_streaming(ordered, min_score, workers=workers, dry_run=dry_run)
    else:
        result = _run_sequential(ordered, min_score, workers=workers, dry_run=dry_run)

    # Summary table
    console.print(f"\n{'=' * 70}")
    summary = Table(title="Pipeline Summary", show_header=True, header_style="bold")
    summary.add_column("Stage", style="bold")
    summary.add_column("Status")
    summary.add_column("Time", justify="right")

    for r in result["stages"]:
        elapsed_str = f"{r['elapsed']:.1f}s"
        status_display = r["status"][:30]
        if r["status"] == "ok":
            style = "green"
        elif r["status"] in ("partial", "skipped"):
            style = "yellow"
        else:
            style = "red"
        summary.add_row(r["stage"], f"[{style}]{status_display}[/{style}]", elapsed_str)

    summary.add_row("", "", "")
    summary.add_row("[bold]Total[/bold]", "", f"[bold]{result['elapsed']:.1f}s[/bold]")
    console.print(summary)

    # Final DB stats
    final = get_stats()
    console.print(f"\n  [bold]DB Final State:[/bold]")
    console.print(f"    Total jobs:     {final['total']}")
    console.print(f"    With desc:      {final['with_description']}")
    console.print(f"    Scored:         {final['scored']}")
    console.print(f"    Ready to apply: {final['ready_to_apply']}")
    console.print(f"    Applied:        {final['applied']}")
    console.print(f"{'=' * 70}\n")

    return result
