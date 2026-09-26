"""
app/models/escalation.py

Stage 11: a log of every escalation to a human leader -- who
triggered it (request_human, needs_support, or the Stage 7/8
reason-capture distress flag), what was said, who got notified, and
when. Kept mainly because it's cheap to record now and becomes real
groundwork for Stage 13 (Leadership Reporting) later, not because the
proposal specifically demands an audit log.
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
    conn.commit()
    cursor.close()
    conn.close()


def create_escalation(reg_number, trigger_type, context_text, notified_leader_reg_number, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO escalations (reg_number, trigger_type, context_text, notified_leader_reg_number, created_at)
        VALUES (%s, %s, %s, %s, %s)
    """, (reg_number, trigger_type, context_text, notified_leader_reg_number, created_at))
    conn.commit()
    cursor.close()
    conn.close()
