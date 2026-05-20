import json
from markdownify import markdownify as md

def clean_greenhouse_json_to_markdown(raw_json_str: str) -> str:
    # 1. Parse the incoming Greenhouse API JSON response string
    data = json.loads(raw_json_str)
    
    dirty_html = data.get("content", "")
    
    if dirty_html:
        # 2. Translate HTML tags to structural plain text markup, omitting attributes
        clean_markdown = md(
            dirty_html, 
            heading_style="ATX",              # Standardizes titles with # tags
            strip=['style', 'script', 'meta'] # Explicitly drop vendor metadata tags
        )
        
        # Fix double/triple trailing newlines left behind by empty paragraph containers
        final_clean_text = "\n".join([line.rstrip() for line in clean_markdown.splitlines() if line.strip()])
        
        # 3. Overwrite the element inside the original structure
        data["content"] = final_clean_text

    # 4. Serialize back into standard JSON layout format
    return json.dumps(data, indent=2)