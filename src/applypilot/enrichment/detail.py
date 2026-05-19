"""Greenhouse API enrichment: fetches full descriptions and apply URLs.

Leverages the public Greenhouse Job Board API to retrieve job details
without the overhead of browser automation or LLM calls.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from applypilot.database import init_db

log = logging.getLogger(__name__)

# -- Greenhouse API logic ----------------------------------------------------

def parse_greenhouse_url(url: str) -> dict | None:
    """Extract board_token and job_id from a Greenhouse URL.
    
    Examples:
      - https://job-boards.greenhouse.io/globalizationpartners/jobs/7692014003
      - https://job-boards.eu.greenhouse.io/agency/jobs/4633535101
      - https://boards.greenhouse.io/andurilindustries/jobs/5090905007?gh_jid=5090905007
    """
    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    path_parts = [p for p in parsed.path.split('/') if p]

    # Detect region
    is_eu = "eu.greenhouse.io" in hostname
    base_url = "https://boards-api.eu.greenhouse.io/v1" if is_eu else "https://boards-api.greenhouse.io/v1"

    # Pattern: /board_token/jobs/job_id
    if len(path_parts) >= 3 and path_parts[1] == "jobs":
        board_token = path_parts[0]
        job_id = path_parts[2]
        return {
            "api_url": f"{base_url}/boards/{board_token}/jobs/{job_id}",
            "board_token": board_token,
            "job_id": job_id
        }

    return None


def fetch_greenhouse_job(url: str, max_retries: int = 3) -> dict:
    """Fetch job details from Greenhouse API with rate limit handling."""
    info = parse_greenhouse_url(url)
    if not info:
        return {"error": "not a recognized Greenhouse URL"}

    retry_count = 0
    base_delay = 2.0

    while retry_count <= max_retries:
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                resp = client.get(info["api_url"])
                
                if resp.status_code == 429:
                    retry_count += 1
                    if retry_count > max_retries:
                        return {"error": "rate limit exceeded after retries"}
                    
                    # Respect headers or back off
                    wait_time = resp.headers.get("Retry-After")
                    if not wait_time:
                        reset_time = resp.headers.get("X-RateLimit-Reset")
                        if reset_time:
                            try:
                                wait_time = max(1, int(reset_time) - int(time.time()))
                            except Exception:
                                wait_time = None
                    
                    if not wait_time:
                        import random
                        wait_time = (base_delay ** retry_count) + (random.random() * 0.5)
                    
                    log.warning("Rate limited (429) for %s. Waiting %.1fs (retry %d/%d)", 
                                info["board_token"], float(wait_time), retry_count, max_retries)
                    time.sleep(float(wait_time))
                    continue

                if resp.status_code == 404:
                    return {"error": "job not found (404)"}
                
                resp.raise_for_status()
                data = resp.json()

                desc = data.get("content")
                if desc:
                    desc = clean_description(desc)

                apply_url = data.get("absolute_url")

                if not desc or not apply_url:
                    return {"error": "incomplete data from API"}

                return {
                    "full_description": desc,
                    "application_url": apply_url,
                    "status": "ok"
                }
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {str(e)}"}
        except Exception as e:
            if retry_count < max_retries:
                retry_count += 1
                wait = base_delay ** retry_count
                log.warning("Error fetching %s: %s. Retrying in %.1fs...", url, e, wait)
                time.sleep(wait)
                continue
            return {"error": str(e)}

    return {"error": "max retries exceeded"}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    """Convert HTML description to clean readable text."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# -- Orchestration -----------------------------------------------------------

def enrich_job(url: str) -> dict:
    """Enrich a single job using the Greenhouse API."""
    t0 = time.time()
    result = fetch_greenhouse_job(url)
    result["elapsed"] = time.time() - t0
    return result


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple],
    delay: float = 0.5,
    max_jobs: int | None = None,
) -> dict:
    """Process all jobs for one site. API based, so low delay."""
    stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    if max_jobs:
        jobs = jobs[:max_jobs]

    if not jobs:
        return stats

    own_conn = conn is None
    if own_conn:
        conn = init_db()

    now = datetime.now(timezone.utc).isoformat()

    for i, (url, title) in enumerate(jobs):
        log.info("[%d/%d] %s", i + 1, len(jobs), title[:50] if title else url[:50])

        result = enrich_job(url)
        stats["processed"] += 1

        status = result.get("status", "error")
        elapsed = result.get("elapsed", 0)
        err_str = result.get("error")

        if status == "ok":
            stats["ok"] += 1
            conn.execute(
                "UPDATE jobs SET full_description = ?, application_url = ?, "
                "detail_scraped_at = ?, detail_error = NULL WHERE url = ?",
                (result.get("full_description"), result.get("application_url"), now, url),
            )
            log.info("  ok | desc=%d chars | %.1fs", len(result["full_description"]), elapsed)
        else:
            stats["error"] += 1
            conn.execute(
                "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
                (err_str or "unknown error", now, url),
            )
            log.info("  error | %s | %.1fs", err_str, elapsed)

        conn.commit()

        if i < len(jobs) - 1 and delay > 0:
            time.sleep(delay)

    if own_conn:
        conn.close()

    return stats


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict:
    """Groups pending jobs by site and processes each batch."""
    where = "WHERE detail_scraped_at IS NULL"
    rows = conn.execute(
        f"SELECT url, title, site FROM jobs {where} ORDER BY site"
    ).fetchall()

    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "error": 0}

    site_jobs: dict[str, list[tuple]] = {}
    for row in rows:
        url, title, site = row[0], row[1], row[2]
        if sites and site not in sites:
            continue
        site_jobs.setdefault(site, []).append((url, title))

    total_stats: dict = {"processed": 0, "ok": 0, "error": 0}

    for site, jobs in site_jobs.items():
        log.info("%s -- %d jobs", site, len(jobs))
        stats = scrape_site_batch(conn, site, jobs, max_jobs=max_per_site)
        
        total_stats["processed"] += stats["processed"]
        total_stats["ok"] += stats["ok"]
        total_stats["error"] += stats["error"]

    log.info("TOTAL: %d processed | %d ok | %d error",
             total_stats["processed"], total_stats["ok"], total_stats["error"])

    return total_stats


def stream_detail(
    upstream_done,
    my_done,
    proxy_str: str | None = None,
    poll_interval: float = 5.0,
) -> None:
    """Streaming detail scraper: polls DB for un-scraped jobs."""
    conn = init_db()

    total_ok = 0
    total_err = 0
    t0 = time.time()

    try:
        while True:
            rows = conn.execute(
                "SELECT url, title, site FROM jobs "
                "WHERE detail_scraped_at IS NULL "
                "ORDER BY site LIMIT 200"
            ).fetchall()

            if rows:
                site_jobs: dict[str, list[tuple]] = {}
                for row in rows:
                    url, title, site = row[0], row[1], row[2]
                    site_jobs.setdefault(site, []).append((url, title))

                for site, jobs in site_jobs.items():
                    stats = scrape_site_batch(conn, site, jobs)
                    total_ok += stats["ok"]
                    total_err += stats["error"]

            upstream_finished = upstream_done is None or upstream_done.is_set()
            if upstream_finished and not rows:
                break
            if not rows:
                time.sleep(poll_interval)
    finally:
        elapsed = time.time() - t0
        log.info("DONE: %d ok, %d errors in %.1fs", total_ok, total_err, elapsed)
        conn.close()
        my_done.set()


def run_enrichment(limit: int = 100, workers: int = 1) -> dict:
    """Main entry point for detail page enrichment."""
    conn = init_db()
    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)
    return stats
