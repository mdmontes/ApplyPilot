# ApplyPilot Codebase Summary

## Project Structure
```
ApplyPilot/
├── analysis/              # Data analysis scripts and notebooks.
├── prompts/               # System and specialized prompts for AI agents.
├── src/
│   └── applypilot/        # Core application source code.
├── test_db/               # SQLite database snapshots for testing.
├── test_scripts/          # Utility scripts for database population and cleanup.
├── AGENTCONTEXT.md        # This file (AI agent context).
├── profile.example.json   # Template for user profile configuration.
└── pyproject.toml         # Build system dependencies and metadata.
```

## Directory Architecture (`src/applypilot/`)
```
src/applypilot/
├── apply/
│   ├── chrome.py          # Manages Chrome browser instances and profiles for automation.
│   ├── dashboard.py       # Rich terminal dashboard for tracking real-time apply progress.
│   ├── launcher.py        # Orchestrator for the apply pipeline, supports Gemini and Claude Code engines.
│   └── prompt.py          # Builds instructions for autonomous agents to fill forms using profile data.
├── config/
│   ├── employers.yaml     # Registry of preconfigured Workday employer portals.
│   ├── searches.example.yaml # Template for user search configurations.
│   └── sites.yaml         # Configuration for direct career sites and extraction rules.
├── discovery/
│   ├── jobspy.py          # Scrapes job boards (Indeed, LinkedIn, etc.) using python-jobspy.
│   ├── smartextract.py    # AI-powered extraction of job details from arbitrary career sites.
│   └── workday.py         # Specialized scraper for Workday-based corporate career portals.
├── enrichment/
│   └── detail.py          # Greenhouse API-focused enrichment of job descriptions and apply URLs.
├── scoring/
│   ├── scorer.py          # LLM-powered job fit scoring (1-10) based on resume/profile.
├── wizard/
│   └── init.py            # Interactive setup wizard for profile, resume, and API configuration.
├── cli.py                 # Typer-based CLI entry point defining all user-facing commands.
├── config.py              # Centralized configuration, environment loading, and path management.
├── database.py            # SQLite database layer, schema definitions, and migration logic.
├── llm.py                 # Unified interface for LLM providers (Gemini, OpenAI, Local).
├── pipeline.py            # Main orchestrator managing sequential or streaming execution of stages.
└── view.py                # Generates the self-contained HTML results dashboard.
```

## Code Execution by Stage

### 1. Discover
- **Purpose**: Identify initial job opportunities and basic metadata.
- **Executed Files**: `discovery/jobspy.py`, `discovery/workday.py`, `discovery/smartextract.py`.
- **Workflow**: Searches job boards and career sites. Deduplicates results by URL and stores them in the `jobs` table.

### 2. Enrich
- **Purpose**: Retrieve the full job description and absolute application URL.
- **Executed Files**: `enrichment/detail.py`.
- **Workflow**: Primarily uses the Greenhouse Job Board API (where applicable) to fetch clean markdown descriptions and direct apply links, avoiding the need for heavy scraping at this stage.

### 3. Score
- **Purpose**: Evaluate how well the candidate fits the job requirements.
- **Executed Files**: `scoring/scorer.py`, `llm.py`.
- **Workflow**: Compares the candidate's plain-text resume against the enriched `full_description`. Assigns a 1-10 `fit_score` and populates `score_reasoning`.

### 4. Apply
- **Purpose**: Autonomously submit the job application.
- **Executed Files**: `apply/launcher.py`, `apply/chrome.py`, `apply/prompt.py`.
- **Workflow**: Acquires high-scoring jobs. Launches an isolated Chrome instance. Uses the Gemini Engine (or Claude Code) to analyze the form, map profile data to fields, and execute the submission.

## SQLite Database & Stage Gating

The `jobs` table in SQLite acts as the central state machine for the pipeline. Each stage is "gated" by specific columns that determine eligibility for processing.

| Stage | Gating Column(s) (Trigger) | Critical Reference Columns (Input) | Populated Columns (Output) |
| :--- | :--- | :--- | :--- |
| **Discover** | N/A (Initial Entry) | Search Config, Site Registries | `url`, `title`, `site`, `location`, `description` |
| **Enrich** | `detail_scraped_at IS NULL` | `url` | `full_description`, `application_url`, `detail_scraped_at` |
| **Score** | `full_description IS NOT NULL` AND `fit_score IS NULL` | `full_description`, Resume/Profile | `fit_score`, `score_reasoning`, `scored_at` |
| **Apply** | `fit_score >= {min}` AND `applied_at IS NULL` | `application_url`, `title`, Profile Data | `applied_at`, `apply_status`, `apply_error`, `apply_attempts` |

### Key Gates & Logic
The pipeline uses specific columns to "gate" jobs between stages. These gates can be bypassed or restricted using flags. 
**Note on CLI Usage**: Flags like `--custom` and `--force` can be used in any order (e.g., `run --custom --force` is identical to `run --force --custom`).

- **Enrichment Gate**: Eligible if `detail_scraped_at IS NULL`.
    - **Restrict to Subset**: Use `--custom` (or `-c`) to only process jobs from `@test/custom_records.sql`.
    - **Bypass Gate (Re-enrich)**: Use `--force` (or `-f`) to ignore the timestamp and re-process.
    - **Example**: `applypilot run enrich --custom --force`
- **Scoring Gate**: Eligible if `full_description IS NOT NULL` AND `fit_score IS NULL`.
    - **Restrict to Subset**: Use `--custom` (or `-c`) to only score jobs from `@test/custom_records.sql`.
    - **Bypass Gate (Re-score)**: Use `--force` (or `-f`) to ignore existing scores and re-evaluate.
    - **Example**: `applypilot run score --custom --force`
- **Apply Gate (Min Score)**: Eligible if `fit_score >= {min_score}` AND `applied_at IS NULL`.
    - **Restrict to Subset**: Use `--custom` (or `-c`) to only apply to jobs from `@test/custom_records.sql`.
    - **Override Score Threshold**: Use `--min-score {number}` to lower/raise the bar (default: 7).
    - **Example**: `applypilot run apply --custom --min-score 5`
- **Data Presence**: The `application_url` must be present (populated during Enrichment) before a job can be applied to.

## Limitations & Considerations
As an AI agent operating within this workspace, my capabilities and limitations are defined as follows:

1. **Explicit Approval Requirement**: I operate primarily in **Plan Mode**, which means I cannot modify source code without first proposing a strategy and obtaining explicit user approval.
2. **Interactive Automation Constraints**: I am unable to interactively execute or debug the browser-based automation tools (Claude Code/Chrome) in real-time. My visibility into the `Auto-Apply` stage is limited to code analysis and logs, as I cannot "see" or interact with the browser sessions directly.
3. **Sandbox Testing**: I cannot easily test features that require external network access to private portals or specialized local environments (like a specific version of Chrome or Node.js) unless automated test suites are provided.
4. **Environment Configuration**: I depend on the existence of valid `.env`, `profile.json`, and `searches.yaml` files for runtime simulation, and I must treat these sensitive files with high security priority.
