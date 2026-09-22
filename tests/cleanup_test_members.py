"""
tests/cleanup_test_members.py

Deletes every member whose reg_number starts with "TEST-" -- the
synthetic rows inserted by tests/manual_db_test.py to verify the
Stage 5 database wiring against the real Render/Neon database.

Run this BEFORE deploying for real use, once real registrations are
expected -- these rows must not be mixed in with genuine members.

Prints what it's about to delete first, and requires typing "yes" to
confirm, since this deletes real rows from the live database.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from app.database import get_connection

if __name__ == "__main__":
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT reg_number FROM members WHERE reg_number LIKE 'TEST-%'")
    rows = cursor.fetchall()

    if not rows:
        print("No TEST- prefixed members found. Nothing to clean up.")
        cursor.close()
        conn.close()
        sys.exit(0)

    print(f"About to delete {len(rows)} test member(s):")
    for row in rows:
        print(f"  {row['reg_number']}")

    confirm = input("\nType 'yes' to delete these from the live database: ")
    if confirm.strip().lower() != "yes":
        print("Cancelled -- nothing deleted.")
        cursor.close()
        conn.close()
        sys.exit(0)

    cursor.execute("DELETE FROM members WHERE reg_number LIKE 'TEST-%'")
    conn.commit()
    print(f"Deleted {cursor.rowcount} test member(s).")

    cursor.close()
    conn.close()
