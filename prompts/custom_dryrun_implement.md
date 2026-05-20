## GOAL

As we speak, we have no real control over which records the applypilot application will use from the sqlitedb during the ‘apply’ stage. If we wanted to test specific records based on the company, or if we wanted to use a small sample size, we are left with no option but to run each job on the entire database. To fix this, we need to create a new command ‘applypilot run apply --dry-run – c’ which will allow us to reference a custom SQL statement that I will manually populate in @test/custom_records.sql

# Files to review
@agentcontex.md
@pipeline.py
@database.py
@config.py
@cli.py
@test/custom_records.sql

# Instructions.
1.	Review all the files above to have an understanding of which critical files will need additions or modifications in order to execute ‘applypilot run apply --dry-run – c’. The goal of the @test/custom_records.sql will be to create a smaller subset of data to do a dry run of job applications based on filters that would be applied in the ‘where’ clause of the sql statement within @test/custom_records.sql
2.	Incorporate some checks to make sure that the dataset created in custom_records.sql does not violate any critical data integrity needed to run the apply job. Checks could include ensure the subset has ALL columns, and that it doesn’t attempt to run a table that is empty, or that is missing data the critical gating column 'application_url'.
3.	Do not run the the new 'applypilot run apply --dry-run -c', 'applypilot run apply --dry-run', not the 'applypilot run apply' commands. In the past this continues to cause the browser to hang without any progress, which I think is due to the your limitations as a CLI agent. However, make sure the code is well integrated
