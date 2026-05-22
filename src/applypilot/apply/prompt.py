"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p.get("personal", {})
    work_auth = p.get("work_authorization", {})
    comp = p.get("compensation", {})
    exp = p.get("experience", {})
    skills = p.get("skills_boundary", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    lines = [
        f"Name: {personal.get('full_name', 'N/A')}",
        f"Email: {personal.get('email', 'N/A')}",
        f"Phone: {personal.get('phone', 'N/A')}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")

    lines.append("")
    auth_status = "Yes" if work_auth.get("legally_authorized_to_work") else "No"
    sponsorship = "Yes" if work_auth.get("require_sponsorship") else "No"
    lines.append(f"Legally Authorized to Work: {auth_status}")
    lines.append(f"Requires Visa Sponsorship: {sponsorship}")

    lines.append("")
    salary_min = comp.get("salary_range_min", comp.get("salary_expectation", "N/A"))
    salary_max = comp.get("salary_range_max", salary_min)
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Desired Salary: {salary_min} - {salary_max} {currency}")
    lines.append(f"Remote Preference: {comp.get('remote_preference', 'Open')}")

    if exp:
        lines.append("")
        lines.append("== EXPERIENCE SUMMARY ==")
        lines.append(f"Years of Exp: {exp.get('years_of_experience_total', 'N/A')}")
        management = "Yes" if exp.get("management_experience") else "No"
        lines.append(f"Management Exp: {management}")
        lines.append(f"Current Title: {exp.get('current_title', 'N/A')}")
        lines.append(f"Target Role: {exp.get('target_role', 'N/A')}")

    if skills:
        lines.append("")
        lines.append("== SKILLS ==")
        if skills.get("programming_languages"):
            lines.append(f"Languages: {', '.join(skills['programming_languages'])}")
        if skills.get("frameworks"):
            lines.append(f"Frameworks: {', '.join(skills['frameworks'])}")
        if skills.get("tools"):
            lines.append(f"Tools: {', '.join(skills['tools'])}")

    if avail:
        lines.append("")
        lines.append(f"Earliest Start Date: {avail.get('earliest_start_date', 'Immediately')}")

    if eeo:
        lines.append("")
        lines.append("== EEO / VOLUNTARY SELF-IDENTIFICATION ==")
        lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
        lines.append(f"Race/Ethnicity: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
        lines.append(f"Veteran Status: {eeo.get('veteran_status', 'Decline to self-identify')}")
        lines.append(f"Disability Status: {eeo.get('disability_status', 'Decline to self-identify')}")

    return "\n".join(lines)


def _build_captcha_section() -> str:
    """Simplified instructions for using the captcha solving tool."""
    return """== CAPTCHA HANDLING ==
If you encounter a CAPTCHA (Turnstile, reCAPTCHA):
1. Use the capsolver MCP tool to create a task and get the token.
2. Inject the token into the page using browser_evaluate.
3. Click Submit."""


def build_prompt(job: dict, dry_run: bool = False) -> str:
    """Build a lean Greenhouse-focused instruction prompt.

    Args:
        job: Job dict from the database.
        dry_run: If True, tell the agent not to click Submit.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    personal = profile["personal"]
    full_name = personal["full_name"]

    # --- Resolve resume PDF path ---
    if not config.RESUME_PDF_PATH.exists():
        raise ValueError(f"Base resume PDF not found at {config.RESUME_PDF_PATH}.")

    # Copy to a clean filename for upload
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(config.RESUME_PDF_PATH), str(upload_pdf))
    pdf_path = str(upload_pdf)

    profile_summary = _build_profile_summary(profile)
    captcha_section = _build_captcha_section()

    # Dry-run override
    submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED." if dry_run else "Click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous agent applying to a Greenhouse job board. 
The application is a SINGLE PAGE. Do not look for login buttons or multi-step pagination.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR DIRECTIVES ==
1. browser_navigate to the job URL.
2. Identify the standard Greenhouse input fields (id="first_name", id="last_name", id="email", id="phone", etc.).
3. Use the browser_fill tool to insert the candidate's exact profile data into these fields.
4. For the Resume upload, use browser_file_upload with this path: {pdf_path}
5. Scroll to the bottom to look for custom questions or EEOC dropdowns (Asterisks * indicate required).
6. If there is a required question you do not know the answer to, deduce it from the profile/resume or enter "N/A".
7. {submit_instruction}

{captcha_section}

== RESULT CODES ==
RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed
RESULT:CAPTCHA -- blocked by captcha
RESULT:FAILED:reason -- any other failure

Do not loop more than 3 times. If the form does not submit after 3 attempts, output RESULT:FAILED."""

    return prompt


def build_gemini_fill_prompt(job: dict, form_structure: str) -> str:
    """Build a prompt for Gemini to map form fields to candidate data.

    Args:
        job: Job dict from the database.
        form_structure: A string representation of the form's fields and labels.
    """
    profile = config.load_profile()
    profile_summary = _build_profile_summary(profile)

    prompt = f"""You are a helpful assistant mapping a job application form to a candidate's profile.
Your goal is to provide a JSON object mapping HTML element IDs to both the appropriate value and the human-readable question/label.

== CANDIDATE PROFILE ==
{profile_summary}

== JOB ==
Title: {job['title']}
Company: {job.get('site', 'Unknown')}

== FORM STRUCTURE ==
{form_structure}

== INSTRUCTIONS ==
1. MANDATORY FIRST STEP: Identify the submission button in the form structure. If you cannot find a way to submit the form, do not attempt to map any fields; instead, return a JSON object with only the "llm_found_submit": "submit_not_found" key.
2. Carefully analyze the form structure provided.
3. For each relevant field (input, select, textarea), determine the best value from the candidate's profile.
4. For standard questions (First Name, Email, Phone, LinkedIn, GitHub, Website), map them accurately.
5. For Work Authorization, EEOC (Gender, Race, Veteran, Disability), and Clearance questions, use the profile to provide honest and consistent answers.
6. For the resume upload field (usually id="resume" or type="file"), use the string "PDF_RESUME_UPLOAD" as the value.
7. VERY IMPORTANT: NEVER provide a value or mapping for "Cover Letter" fields. They should always be ignored.
8. ALWAYS answer "No" to any questions asking if the candidate has worked at the hiring company before, is a former employee, or has previously been employed by this organization.
9. ALWAYS answer "Yes" or consent to any questions regarding Privacy Policies, Data Processing, or Applicant Privacy Notices.
10. For questions about working "On-site", "Hybrid", or "Relocation", answer "Yes" ONLY if the job location is in North Carolina, USA. Otherwise, answer "No".
11. ALWAYS set pronouns to "he/him" if asked.
12. ALWAYS state that the candidate is based in the "United States" (or USA/US) if asked where they are based, where they reside, or their current location. Use the specific city/state from the profile if a more granular location is required, but ensure the country is clearly the United States.
13. For questions asking how the candidate heard about the job or the source of their application, ALWAYS answer "From the company jobs site" or select the equivalent option if a list is provided.
14. If a field is required (*) and you don't have a specific value, provide a reasonable default (e.g., "N/A" or "0").
15. IDENTIFY THE SUBMIT BUTTON: Confirm the exact ID or selector of the submission button.
16. REPORT SUBMISSION STATUS: Include a special top-level key in your JSON response named "llm_found_submit". Assign it one of the following values:
    - "submit_not_found": If you cannot identify a clear submission button.
    - "submit_found_not_pressed": If you identified a submission button but are NOT instructing to press it (standard mapping phase or dry-run).
    - "submit_pressed": If you identified the button and instructions indicate it should be pressed.
17. Return a JSON object where keys are the EXACT HTML IDs provided, and values are nested objects containing "value" and "label".
18. The "label" MUST be the human-readable question or field name found in the form structure.

Example output:
{{
  "llm_found_submit": "submit_found_not_pressed",
  "first_name": {{
    "value": "John",
    "label": "First Name"
  }},
  "email": {{
    "value": "john.doe@example.com",
    "label": "Email Address"
  }},
  "resume_input": {{
    "value": "PDF_RESUME_UPLOAD",
    "label": "Resume/CV"
  }},
  "question_123": {{
    "value": "Yes",
    "label": "Are you legally authorized to work in the US?"
  }}
}}
"""
    return prompt
