## Goal
 As we speak, we need to modify the ‘enrich’ phase of our applypilot stage to include critical data that the public greenhouse apis’ provide per job request.

## Instructions
1.	We need to modify the enrich logic in @src/applypilot/detail.py so that we pass a parameter ‘?questions=true’ to the end of the greenhouse base urls. For a ‘url’ such as “https://www.esri.com/careers/5091602007?gh_jid=5091602007”, we would need to make sure we can resolve ‘https://boards-api.greenhouse.io/v1/boards/esri/jobs/5091602007?questions=true’.
2.	We need to create a new column in the sqlite database between ‘full_description’ and ‘application_url’ called ‘greenhouse_api_url’. Moving forward, this column is going to house the greenhouse API url. 
3.	We currently already extract the ‘full_description” and “application_url” from a greenhouse get request. We need to make sure we are extracting this from the new ‘greenhouse_api_url’. If we cannot, then we should try extracting it from the version of the base URL that does not have the ‘?questions=true’ parameter. If none can be found, then less throw an error for this specific application as ‘app details not available’, populate the ‘detail_error’ column in sqlite, and ensure that the parsing moves on to the next job application.
4.	We need to create a new column in the sqlite database between ‘detail_error’ and ‘fit_score’ called ‘application_schema’. Moving forward, this column is going to store the response from the ‘greenhouse_api_url’. A sample response of what should be stored is available in @test_scripts/sample_application_details_getresponse.JSON.
5.	Update @database.py and any other files where we inject sql to make sure the new columns are accounted for.
