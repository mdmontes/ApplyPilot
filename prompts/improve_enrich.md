## GOAL

As we speak, our @src/applypilot/enrichment/details.py code is able to extract the ‘urls’ from the sqlitedb and implements a basic parsing logic of these URLs that allows it to generate a get request to a greenhouse public api, which follows the standard pattern, https://boards-api.greenhouse.io/v1/boards/<company_name>/jobs/<job_id>. Unfortunately, however, the parsing logic is not perfect, and often errors out when it sees a ‘weird’ url.  One such weird URL is ‘https://www.brex.com/careers/8366850002?gh_jid=8366850002’. The logic is not able to resolve a greenhouse get request api to a url https://boards-api.greenhouse.io/v1/boards/ brex/jobs/8366850002. Therefore, we need to improve the parsing logic in details.py

# Files to review
@src/applypilot/enrichment/details.py
@test_scripts/weird_urls.md

# Instructions.
1.	Review @src/applypilot/enrichment/details.py to understand the current url parsing logic and construction of greenhouse ‘get’ request URLS
2.	Review @test_scripts/weird_urls.md and examine any additional patterns in how we could extract the company name and job id from the ‘URL’ column of the sqlite database to call the greenhouse API and extract the proper job description
3.	I will proceed to test using the applypilot run enrich -f -c command.
