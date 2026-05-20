## GOAL
To modify the function clean_description() from @src/applypilot/enrichment/detail.py to improve how we clean the 'content' element from the greenhouse api JSON response, and ensure that this data properly populates the 'full_description' column of the sqlite dtabase. Currently, the clean_description() is still leaving many html tags and other unwanted namespace characters. 

## Files to review
@src/applypilot/enrichment/detail.py
@test_scripts/improve_clean_description.py
@src/applypilot/scoring/score.py

## Instructions:
1. Delete all the data within the sqlitedatabase for the column 'full_description'

2. Delete all the data within the sqlitedatabase for the column 'application_url'

3. After studying the current clean_description() function of @src/applypilot/enrichment/detail.py, come up with a way to incorporate a better cleaning logic that will also improve an LLM's ability to read the information within the 'full_description' column of the sqlite db. Use @test_scripts/improve_clean_description.py as a template. Make sure that the sql query into the sqlite db is parameterized.

4. Modify @src/applypilot/scoring/score.py so that it can digest the new description updates.

5. Ensure that the sqlite database is set up so that I can run 'applypilot run enrich' again without running into errors. I will run this to test the implementation of your code
