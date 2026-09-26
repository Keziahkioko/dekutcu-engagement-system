"""
app/models/pending_fellowship_checkin.py

Stage 7: tracks an outstanding "were you at fellowship today?"
question sent to a member -- one row means that member has been asked
and hasn't replied (yet). checkin_date is the date the question was
sent (same evening as the fellowship itself); get_stale_pending_checkins
finds rows still unanswered by the next day, for the noon sweep.
"""

from app.database import get_connection


def init_pending_fellowship_checkin_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_fellowship_checkin (
            whatsapp_id TEXT PRIMARY KEY,
            activity_type TEXT NOT NULL,
            checkin_date DATE NOT NULL,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_fellowship_checkin(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_fellowship_checkin WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_fellowship_checkin(whatsapp_id, activity_type, checkin_date, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_fellowship_checkin (whatsapp_id, activity_type, checkin_date, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET activity_type = EXCLUDED.activity_type, checkin_date = EXCLUDED.checkin_date, created_at = EXCLUDED.created_at
    """, (whatsapp_id, activity_type, checkin_date, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_fellowship_checkin(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_fellowship_checkin WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()


def get_stale_pending_checkins():
    """Every check-in question sent on an earlier calendar day, still unanswered."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_fellowship_checkin WHERE checkin_date < CURRENT_DATE"
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows
