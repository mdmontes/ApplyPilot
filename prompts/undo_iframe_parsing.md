We need to refactor the HTML parsing layer of the ApplyPilot application. A recent update introduced complex HTML parsing logic specifically designed to handle deeply embedded `iframe` structures for ATS platforms like Greenhouse in @src/applypilot/launcher.py and @src/applypilot/prompt.py

This update significantly degraded performance and introduced unnecessary complexity. I want to entirely remove the iframe-specific parsing logic and revert our approach back to the standard "Partial DOM Strategy."

Please review the current codebase and:
1. Strip out any iframe traversal, switching, or deep nesting parsing logic.
2. Ensure the core Playwright programmatic JavaScript injection remains intact.
3. Keep the clean, flattened DOM structure that gets passed to the Gemini LLM logic layer for final field identification whenever the javacript injection cannot populate all fields.
4. Optimize for execution speed, ensuring we aren't getting bogged down in heavily customized, non-standard application layouts.
5. Once code has been updated, do not run any applypilot commands to account for the fact that you cannot test these due to your cli limitations

Let's look at the parsing engine files first and plan the code removal.