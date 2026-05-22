## Goal
As we speak, we have made some powerful efficiencies within the 'apply' stage of our current instance of applypilot by switching all of our llm calls away from claude and into gemini, as well as by focusing our logic around populating semi-standardized one-page application set ups from the Greenhouse ats system. To improve efficiencies further, we need to minimize the reasoning workload that the LLM is exercising to populate the application, and implement a 'Zero-DOM-Parsing Strategy' so that we can populate as many fields programmatically and 'LLM free'. The larger goal is to make application submissions much cheaper, improving the cost of applications from their current cost of 10 cents per submission, to 1 cent per submission. Furthermore, we need to make some additional modifications to the 'applypilot run apply' 'applypilot run apply --dry-run' and 'applypilot run apply --dry-run c' so that the 'apply' stage can run headless moving forward, and so that we can log additional application details into the sqlite database. Lastly, we need to ensure that the logs rendered during the 'apply' stage are properly reporting the costs of these applications, which currently they are not.

## folders to review:
-	All files within @src/applypilot/apply
-	@profile.example (currently keeps the structure of real file being used which is not immediately accessible within this codebase @C:\Users\manue\.applypilot\profile.json)
-	@agentcontex.md
-	@pipeline.py
-	@database.py
-	@config.py
-	@cli.py
-	@test_scripts/alt.html
-	@ test_scripts/agency.html
-	@test_scripts/jobs_sqlite_structure.sql

# Instructions for Zero-DOM-Parsing Strategy:

1.	Study all of the code under src/applypilot/apply, particularly, @launcher.py and @prompt.py to understand how the code is currently structured.
2.	Study @profile.example, which currently possesses the same structure as the actual file containing my personal information for job applications stored in '@C:\Users\manue\.applypilot\profile.json'.
3.	Study @test_scripts/alt.html and @test_scripts/agency.html, as they are sample greenhouse application HTML files, which introduce examples of the types of fields for greenhouse applications that will likely be consistent enough for us to fill out programmatically, as well as those that we will have to fill out using the gemini LLM. Understand these patterns.
4.	Modify the appropriate files so that we can include a Zero-Dom Parsing strategy which populates the application programmatically by mapping the values from my personal profile to the application. Inject a static JavaScript mapping routine that binds standard Greenhouse IDs (#first_name, #email, #job_application_resume, etc.) straight to the browser context variables. The logic should default to having the LLM attempt to populate the fields if the DOM is strange and errs out, or if the application contains questions that cannot be answered simply using my 'profile.JSON'. Use launcher_zerodom_template.py as a template. 

# Additional instructions to modify the 'apply' stage and create a new 'application_details' column in sqlitedatabase to track applications:
1.	Run a custom script that will add a new column to the sqlitedatabase called 'application_details' of datatype 'text'.
2.	After studying @src/applypilot/apply, study @agentcontex.md, @pipeline.py, @database.py, @config.py and @cli.py, focusing on how the apply stage runs and how it impacts the existing database.
3.	Modify the files above so that we can seamlessly integrate also populating the column 'application_details' when we run the 'apply' stage. This column should house a dictionary that would capture all the application details that the files under @scr/applypilot/apply would populate. The dictionary's keys would be the greenhouse application fields like 'first_name', 'last_name' and everything from top to bottom. The values for this dictionary would constitute what our application filled out. This will save us from having to visualize what was populated opening up a browser, and allows the apply stage to run headless. The file @test_scripts/jobs_sqlite_structure.sql has been modified to reflect this structure
4.	Make sure that the 'dry-run' instances of 'applypilot run apply' continue to avoid any submissions. Moving forward, all applypilot run apply commands should run headless (no launching the browser on my desktop for me to see)
5.	Review how we are currently logging the activity we run when we the applypilot stage. Previously, we could see the cost of each application fill out, but lately, this seems to have stopped working.

## last details
1. Do not run any 'applypilot run apply' jobs. These usually hang do to your cli limitations. I will have to test the code myself after you implement.
