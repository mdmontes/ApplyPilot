import sqlite3
import os
from pathlib import Path

db_path = Path.home() / ".applypilot" / "applypilot.db"

if not db_path.exists():
    print(f"Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# 1. Check total rows in jobs
total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
print(f"Total jobs in DB: {total}")

# 2. Check site names available
sites = conn.execute("SELECT DISTINCT site FROM jobs").fetchall()
print(f"Available sites in DB: {[s['site'] for s in sites]}")

# 3. Test the specific query
custom_q = "Select * from jobs where site in ('andurilindustries', 'hyphenconnect')"
matches = conn.execute(custom_q).fetchall()
print(f"Matches for custom query: {len(matches)}")

# 4. Check eligibility (fit_score >= 7, applied_at is null, application_url is not null)
if matches:
    print("\nEligibility check for matches:")
    for row in matches[:3]:
        print(f"- URL: {row['url'][:50]}...")
        print(f"  Score: {row['fit_score']}, Applied: {row['applied_at']}, App URL: {row['application_url']}")

conn.close()
