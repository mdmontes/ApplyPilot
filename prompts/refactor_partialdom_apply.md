## Goal
As we speak, we have made some powerful efficiencies within the ‘apply’ implement after attempting to apply a ‘Zero-DOM-Parsing Strategy’. Unfortunately, not every page that we visited allowed us to populate most of the fields using the ‘Zero-DOM-Parsing strategy’, as some common fields like “first name” ‘last name” did where not available for us to inject using a Javascript mapping routine to bind against standard Greenhouse IDs. Therefore, we need to implement a ‘DOM-Parsing-As-Needed’ strategy.

## folders to review:
-	All files within @Src/applypilot/apply
-	@profile.example (currently keeps the structure of real file being used which is not immediately accessible within this codebase @C:\Users\manue\.applypilot\profile.json
-	@pipeline.py
-	@database.py
-	@config.py
-	@cli.py
-	@test_scripts/alt.html
-	@test_scripts/agency.html
-	@test_scripts/target_applicaton_details.

## Instructions for DOM-Parsing-As-Needed Strategy adjustments:
1.	Study the code under Src/applypilot/apply, particularly, @launcher.py and @prompt.py to understand how the code is currently structured.
2.	Study @profile.example, which currently possesses the same structure as the actual file containing my personal information for job applications stored in ‘@C:\Users\manue\.applypilot\profile.json’.
3.	We have seen that the ‘Zero-DOM-Parsing’ strategy is quite strict and leaves many applications unanswered. Therefore, we will need to implement logic that 1) checks to see if programmatically we could populate all the questions as shown in the file @test_scripts/target_applicaton_details. If we cannot inject all such questions, then we should default to 1) mapping out those questions that we can be injected into the chrome context 2) map out those questions we cannot inject and proceed to  scan the DOM with the LLM to populate the answers. The logic should ideally incorporate both programmatic and LLM based DOM parsing and population, with programmatic injection being the first layer of application population attempts, and the LLM resolving anything that cannot be populated programmatically. We should be able to use @test_scripts/target_applicaton_details to map our answers to the form using the LLM.
4.	If we estimate that an application might cost more than 5 cents per application, lets skip it, return any application_details we where able to resolve, and submit an entry to the column ‘apply_error’ with a string format ‘application too expensive’.
5.	Study @test_scripts/alt.html and @test_scripts/agency.html, as they are sample greenhouse application HTML files, which introduce examples of the types of fields for greenhouse applications DOM that will likely be consistent enough for us fill out using the gemini LLM.
6.	When we populate “application_details” in the sqlitedatabase we should strive to provide a profile that matches the quality of @test_scripts/target_applicaton_details.JSON, more so than what we are currently doing, whereby we are generating a response like @test_scripts/current_application_details.JSON. The ‘current_application_details’ have entries like ‘question_11651473007’ which we have no idea what that is.
