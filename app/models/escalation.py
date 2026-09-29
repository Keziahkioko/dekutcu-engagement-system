"""
app/models/escalation.py

Stage 11: a log of every escalation to a human leader -- who
triggered it (request_human, needs_support, or the Stage 7/8
reason-capture distress flag), what was said, who got notified, and
when. Kept mainly because it's cheap to record now and becomes real
groundwork for Stage 13 (Leadership Reporting) later, not because the
proposal specifically demands an audit log.

escalation_cases (added after the first live test, 2026-09-29): one
escalation can notify SEVERAL leaders (every exec leader, when the
member has no group leader), and they could all end up contacting the
same struggling member at once. A case groups those notifications so
the first leader to reply CLAIM takes it and the others stand down.
claimed_by/claimed_at also give Stage 13 real response-time data.
Each `escalations` row (one per notified leader) points at its case.
"""

from app.database import get_connection


def init_escalations_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS escalations (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            context_text TEXT,
            notified_leader_reg_number TEXT,
            created_at TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS escalation_cases (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            context_text TEXT,
            created_at TIMESTAMP,
            claimed_by_reg_number TEXT,
            claimed_at TIMESTAMP
        )
    """)
    cursor.execute("ALTER TABLE escalations ADD COLUMN IF NOT EXISTS case_id INTEGER REFERENCES escalation_cases(id)")
    # Stage 13 -- follow-up of UNCLAIMED cases (see
    # escalation.follow_up_unclaimed_cases): urgency sets the timings
    # (acute is followed up faster and ignores quiet hours); reminded_at
    # and backstop_at make sure each step happens exactly once.
    cursor.execute("ALTER TABLE escalation_cases ADD COLUMN IF NOT EXISTS urgency TEXT DEFAULT 'normal' NOT NULL")
    cursor.execute("ALTER TABLE escalation_cases ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMP")
    cursor.execute("ALTER TABLE escalation_cases ADD COLUMN IF NOT EXISTS backstop_at TIMESTAMP")
    conn.commit()
    cursor.close()
    conn.close()


def create_escalation(reg_number, trigger_type, context_text, notified_leader_reg_number, created_at, case_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO escalations (reg_number, trigger_type, context_text, notified_leader_reg_number, created_at, case_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (reg_number, trigger_type, context_text, notified_leader_reg_number, created_at, case_id))
    conn.commit()
    cursor.close()
    conn.close()


def create_case(reg_number, trigger_type, context_text, created_at, urgency="normal"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO escalation_cases (reg_number, trigger_type, context_text, created_at, urgency)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """, (reg_number, trigger_type, context_text, created_at, urgency))
    case_id = cursor.fetchone()["id"]
    conn.commit()
    cursor.close()
    conn.close()
    return case_id


def claim_case(case_id, leader_reg_number, claimed_at):
    """
    Atomic: only succeeds if nobody has claimed it yet, so two leaders
    replying CLAIM at the same moment can't both "win". Returns True if
    THIS call claimed it.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE escalation_cases SET claimed_by_reg_number = %s, claimed_at = %s
        WHERE id = %s AND claimed_by_reg_number IS NULL
        RETURNING id
    """, (leader_reg_number, claimed_at, case_id))
    claimed = cursor.fetchone() is not None
    conn.commit()
    cursor.close()
    conn.close()
    return claimed


def get_case(case_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM escalation_cases WHERE id = %s", (case_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def get_recent_case(reg_number, since):
    """This member's most recent case created at or after `since`, or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM escalation_cases WHERE reg_number = %s AND created_at >= %s
        ORDER BY created_at DESC LIMIT 1
    """, (reg_number, since))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def notified_leaders(case_id):
    """Reg numbers of every leader notified on this case."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT notified_leader_reg_number FROM escalations WHERE case_id = %s", (case_id,))
    rows = [r["notified_leader_reg_number"] for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    return rows


def open_cases_for_leader(leader_reg_number):
    """Unclaimed cases this leader was notified on, newest first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT c.* FROM escalation_cases c
        JOIN escalations e ON e.case_id = c.id
        WHERE e.notified_leader_reg_number = %s AND c.claimed_by_reg_number IS NULL
        ORDER BY c.created_at DESC
    """, (leader_reg_number,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_unclaimed_cases():
    """Every case nobody has claimed yet, oldest first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM escalation_cases WHERE claimed_by_reg_number IS NULL ORDER BY created_at")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def mark_case(case_id, column, at):
    """Records that a follow-up step happened -- column is 'reminded_at' or 'backstop_at'."""
    assert column in ("reminded_at", "backstop_at")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE escalation_cases SET {column} = %s WHERE id = %s", (at, case_id))
    conn.commit()
    cursor.close()
    conn.close()
