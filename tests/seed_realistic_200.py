"""
tests/seed_realistic_200.py

One-off manual seed: inserts ~199 synthetic members (reg_number
prefixed "TEST-", same as tests/manual_db_test.py, so they're safe to
find and delete later) using the REAL DeKUTCU area names and realistic
distribution weights from generate_sample_members.py -- so the total
member count (199 + Keziah's 1 real member) is a genuine ~200-member
simulation of a real deployment, not the isolated TestVille/TestTown
wiring check from before.

Batches the insert into a single connection (one multi-row INSERT)
instead of one connection per member -- 35 individual inserts already
took over 60s on Render's free tier; 199 individually would take
several minutes for no benefit.

Leaves the rows in place on purpose -- see tests/cleanup_test_members.py.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from generate_sample_members import generate_members
from app.database import get_connection

if __name__ == "__main__":
    members = generate_members(total_members=199, seed=7)  # different seed from the CSV (42), so this is a distinct draw

    conn = get_connection()
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            f"TEST-{m['member_id']:03d}",
            None,
            f"TEST {m['name']}",
            "Male" if m["gender"] == "M" else "Female",
            m["year_of_study"],
            m["area"],
            True,
            True,
            now,
        )
        for m in members
    ]

    cur.executemany("""
        INSERT INTO members (
            reg_number, whatsapp_id, name, gender, year_of_study, area,
            data_consent, followup_consent, registered_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, rows)

    conn.commit()
    print(f"Inserted {len(rows)} synthetic members with real area names.")

    cur.execute("SELECT COUNT(*) AS n FROM members WHERE data_consent = TRUE")
    print(f"Total data-consenting members now in the database: {cur.fetchone()['n']}")

    cur.close()
    conn.close()
