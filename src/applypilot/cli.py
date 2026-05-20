"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "apply")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Note: 'discover' is currently experimental for Greenhouse. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for auto-apply eligibility."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    custom: bool = typer.Option(False, "--custom", "-c", help="Use custom query from test/custom_records.sql"),
    force: bool = typer.Option(False, "--force", "-f", help="Ignore gating columns (e.g. re-enrich or re-score)."),
) -> None:
    """Run pipeline stages: discover, enrich, score."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    if "score" in stage_list or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring")

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        custom=custom,
        force=force,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("gemini-2.0-flash", "--model", "-m", help="Gemini model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 2 required (LLM API key + Chrome)
    check_tier(2, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Scored jobs exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? AND applied_at IS NULL",
            (min_score,)
        ).fetchone()[0]
        if ready == 0:
            console.print(
                f"[red]No jobs with score >= {min_score} found.[/red]\n"
                "Run [bold]applypilot run score[/bold] first to identify jobs to apply for."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks (Optional/Manual) ---
    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP - still useful for other things maybe, but let's keep it as optional)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", "[dim]optional[/dim]",
                        "Install Node.js 18+ for playwright utilities"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters, and auto-apply (needs LLM API key + Chrome)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 2 satisfies all current requirements.[/dim]")

    console.print()


if __name__ == "__main__":
    app()
