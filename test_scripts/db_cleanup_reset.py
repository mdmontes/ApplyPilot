import sqlite3
import os
from pathlib import Path

# Try to find the DB path similarly to how the app does
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))
DB_PATH = APP_DIR / "applypilot.db"

def cleanup():
    db_path = DB_PATH
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        # Try local test_db if it exists
        local_db = Path("test_db/applypilot.db")
        if local_db.exists():
             print(f"Found local database at {local_db}")
             db_path = local_db
        else:
            return

    print(f"Connecting to {db_path}...")
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE jobs SET 
                full_description = NULL, 
                application_url = NULL, 
                detail_scraped_at = NULL, 
                detail_error = NULL
        """)
        print(f"Updated {cursor.rowcount} rows.")
        conn.commit()
    except Exception as e:
        print(f"Error during cleanup: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    cleanup()
