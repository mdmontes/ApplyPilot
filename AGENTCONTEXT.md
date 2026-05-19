# ApplyPilot Codebase Summary

## Directory Architecture
```
src/applypilot/
├── apply/
│   ├── chrome.py          # Manages Chrome browser instances and profiles for automation.
│   ├── dashboard.py       # Provides real-time tracking and a live dashboard for the auto-apply stage.
│   ├── launcher.py        # Main entry point for the apply pipeline, orchestrating Claude Code sessions.
│   └── prompt.py          # Generates and manages the complex prompts used to guide Claude Code through forms.
├── config/
│   ├── employers.yaml     # Registry of preconfigured Workday employer portals.
│   ├── searches.example.yaml # Template for user search configurations.
│   └── sites.yaml         # Configuration for direct career sites and extraction rules.
├── discovery/
│   ├── jobspy.py          # Scrapes job boards (Indeed, LinkedIn, etc.) using the python-jobspy library.
│   ├── smartextract.py    # AI-powered extraction of job details from non-standard career sites.
│   └── workday.py         # Specialized scraper for Workday-based application portals.
├── enrichment/
│   └── detail.py          # Fetches and extracts full job descriptions via a 3-tier cascade (JSON-LD, CSS, AI).
├── scoring/
│   ├── scorer.py          # AI logic for rating job fit on a scale of 1-10.
├── wizard/
│   └── init.py            # Implementation of the `applypilot init` setup wizard.
├── cli.py                 # The Typer-based CLI entry point defining all user commands.
├── config.py              # Centralized configuration management and profile loading logic.
├── database.py            # SQLite database layer, schema definitions, and connection management.
├── llm.py                 # Unified interface for various LLM providers (Gemini, OpenAI, Local).
├── pipeline.py            # Orchestrator that manages the sequential or concurrent execution of pipeline stages.
└── view.py                # Generates the self-contained HTML results dashboard for pipeline status.
```

## Code Execution by Stage

### 1. Discover
- **Purpose**: Identify job opportunities from various sources.
- **Executed Files**: `discovery/jobspy.py`, `discovery/workday.py`, `discovery/smartextract.py`.
- **Workflow**: `pipeline.py` calls these modules to search job boards, scrape Workday portals, and hit direct career sites. Results are deduplicated and stored in the database via `database.py`.

### 2. Enrich
- **Purpose**: Retrieve the full job description and application URL for every discovered job.
- **Executed Files**: `enrichment/detail.py`.
- **Workflow**: Visits each job URL and uses a 3-tier approach: structured data (JSON-LD), CSS selectors, or LLM-assisted extraction to get the full text needed for scoring.

### 3. Score
- **Purpose**: Rate every job (1-10) to determine which ones match the user's profile.
- **Executed Files**: `scoring/scorer.py`, `llm.py`.
- **Workflow**: Compares the enriched job description against the user's profile using an LLM. Only jobs meeting a certain threshold proceed.

### 4. Apply
- **Purpose**: Submit the application autonomously.
- **Executed Files**: `apply/launcher.py`, `apply/chrome.py`, `apply/prompt.py`, `apply/dashboard.py`.
- **Workflow**: `launcher.py` coordinates browser instances via `chrome.py`. It uses `prompt.py` to instruct Claude Code to navigate forms, fill details, and upload the base resume. Progress is monitored via `dashboard.py`.

## Limitations & Considerations
As an AI agent operating within this workspace, my capabilities and limitations are defined as follows:

1. **Explicit Approval Requirement**: I operate primarily in **Plan Mode**, which means I cannot modify source code without first proposing a strategy and obtaining explicit user approval.
2. **Interactive Automation Constraints**: I am unable to interactively execute or debug the browser-based automation tools (Claude Code/Chrome) in real-time. My visibility into the `Auto-Apply` stage is limited to code analysis and logs, as I cannot "see" or interact with the browser sessions directly.
3. **Sandbox Testing**: I cannot easily test features that require external network access to private portals or specialized local environments (like a specific version of Chrome or Node.js) unless automated test suites are provided.
4. **Environment Configuration**: I depend on the existence of valid `.env`, `profile.json`, and `searches.yaml` files for runtime simulation, and I must treat these sensitive files with high security priority.
