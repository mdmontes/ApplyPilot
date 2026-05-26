import json
import logging
from applypilot.llm import get_client
from applypilot.database import get_connection

logger = logging.getLogger(__name__)

def check_prepopulated_app(job: dict, profile: dict, prepopulated: list[dict], model: str | None = None) -> list[dict]:
    """Use Gemini to verify and complete the prepopulated application map."""
    llm = get_client()
    
    schema_str = job.get("application_schema")
    profile_json = json.dumps(profile, indent=2)
    prepopulated_json = json.dumps(prepopulated, indent=2)
    
    prompt = f"""You are a job application expert. Your goal is to verify and complete a prepopulated application map.

== APPLICATION SCHEMA (GREENHOUSE API RESPONSE) ==
{schema_str}

== CANDIDATE PROFILE ==
{profile_json}

== CURRENT PREPOPULATED MAP ==
{prepopulated_json}

== INSTRUCTIONS ==
1. Analyze the Application Schema and the Candidate Profile.
2. Review the Current Prepopulated Map.
3. Verify that all answers are correct according to the profile.
4. For any field marked "answer_not_found", analyze if you can find the answer in the profile or resume context provided in the profile.
5. IMPORTANT: Ensure a path to the resume is provided for any resume/CV field, regardless of whether it is required.
6. IMPORTANT: ALWAYS provide the LinkedIn profile link for any field asking for LinkedIn (e.g., "LinkedIn", "LinkedIn Profile", "LinkedIn URL"), regardless of whether it is required.
7. IMPORTANT: ALWAYS provide the GitHub profile link for any field asking for GitHub, Website, Portfolio, or any other personal link (e.g., "GitHub", "Personal Website", "Portfolio", "Website URL"), regardless of whether it is required.
8. VERY IMPORTANT: NEVER provide a path or answer for any "Cover Letter" questions. Cover letters should ALWAYS be skipped. If you see a cover letter field, ensure its applicant_answer is "do_not_fill" or empty.
9. ALWAYS answer "No" to any questions asking if the candidate has worked at the hiring company before, is a former employee, or has previously been employed by this organization.
10. ALWAYS answer "Yes" or consent to any questions regarding Privacy Policies, Data Processing, or Applicant Privacy Notices.
11. For questions about working "On-site", "Hybrid", or "Relocation", answer "Yes" ONLY if the job location (from the schema or job context) is in North Carolina, USA. Otherwise, answer "No".
12. ALWAYS set pronouns to "he/him" if asked.
13. ALWAYS state that the candidate is based in the "United States" (or USA/US) if asked where they are based, where they reside, or their current location. Use the specific city/state from the profile if a more granular location is required, but ensure the country is clearly the United States.
14. For questions asking how the candidate heard about the job or the source of their application, ALWAYS answer "From the company jobs site" or select the equivalent option if a list is provided.
15. The resume path should be exactly what was in the input map if found, or a standard placeholder if missing (e.g., from the profile).
16. Correct any errors in the current map.
17. Ensure all "required" questions have a valid "applicant_answer". If no answer can be found even by you, provide a sensible default like "N/A" or "0" if appropriate, but try your best to find a real answer.
18. Return the FINAL, CLEANED map as a JSON array of objects. Each object MUST have the same keys: "id", "label", "required", "type", and "applicant_answer".

Output ONLY the JSON array.
"""
    
    response = llm.ask(prompt, model=model)
    
    try:
        json_str = response.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()
            
        final_map = json.loads(json_str)
        
        # Write to database
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET application_prepopulated = ? WHERE url = ?",
            (json.dumps(final_map), job["url"])
        )
        conn.commit()
        
        return final_map
    except Exception as e:
        logger.error(f"Error parsing LLM response for prepopulated app check: {e}")
        return prepopulated
