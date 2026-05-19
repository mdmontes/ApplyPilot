import sqlite3
import pandas as pd
from datetime import datetime, timezone
import sys
from pathlib import Path

# Add src to path so we can import applypilot
sys.path.append(str(Path(__file__).parent.parent / "src"))

try:
    from applypilot.config import DB_PATH, load_env, ensure_dirs
    from applypilot.database import init_db
except ImportError:
    print("Could not import applypilot. Ensure you are running from the project root.")
    sys.exit(1)

def main():
    load_env()
    ensure_dirs()
    
    # Ensure database is initialized with current schema
    init_db(DB_PATH)

    # Load specific Greenhouse dataset
    csv_path = Path('greenhouse_05182026.csv')
    if not csv_path.exists():
        print(f"Error: {csv_path} not found.")
        return
        
    # Use pandas to read the CSV. It should handle the quotes reasonably well.
    # We will strip extra quotes just in case.
    df = pd.read_csv(csv_path)

    # Connect to ApplyPilot's local DB
    print(f"Connecting to database at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Clear existing jobs for a clean Greenhouse-only state
    print("Clearing existing jobs...")
    cursor.execute("DELETE FROM jobs")

    now = datetime.now(timezone.utc).isoformat()
    count = 0
    
    print(f"Populating {len(df)} Greenhouse jobs...")
    for index, row in df.iterrows():
        try:
            # Strip extra quotes if they exist (CSV has triple double-quotes)
            url = str(row['URL']).strip('"').strip("'")
            title = str(row['JOB_TITLE']).strip('"').strip("'")
            site = str(row['COMPANY']).strip('"').strip("'")
            location = str(row['LOCATION']).strip('"').strip("'")

            if not url or url == 'nan':
                continue

            # For Greenhouse, the URL is often the application URL or contains it
            # We'll populate both to be safe
            cursor.execute("""
                INSERT INTO jobs (url, title, site, location, strategy, discovered_at, fit_score, application_url) 
                VALUES (?, ?, ?, ?, ?, ?, 10, ?)
            """, (
                url, 
                title, 
                site, 
                location, 
                'greenhouse', 
                now,
                url
            ))
            count += 1
        except sqlite3.IntegrityError:
            # Skip duplicates if any
            pass
        except Exception as e:
            print(f"Error inserting row {index}: {e}")

    conn.commit()
    conn.close()
    print(f"Successfully loaded {count} Greenhouse jobs for processing.")

if __name__ == "__main__":
    main()
