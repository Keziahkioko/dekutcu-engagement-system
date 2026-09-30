"""
app/models/pending_attendance_marking.py

Stage 7: tracks an outstanding "who was absent from Bible Study today?"
question sent to a group leader -- one row means that leader has been
asked and hasn't replied yet. group_label + activity_date are recorded
so the reply resolves against the right roster and the right date,
even if the leader doesn't reply the same evening.
"""

from app.database import get_connection


def init_pending_attendance_marking_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_attendance_marking (
            whatsapp_id TEXT PRIMARY KEY,
            group_label TEXT NOT NULL,
            activity_date DATE NOT NULL,
            created_at TIMESTAMP
        )
    """)
    # Whether the leader has had the one gentle "you haven't marked it yet"
    # reminder (added 2026-09-30 -- see attendance.take_reminder).
    cursor.execute("ALTER TABLE pending_attendance_marking ADD COLUMN IF NOT EXISTS reminded BOOLEAN DEFAULT FALSE NOT NULL")
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_attendance_marking(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_attendance_marking WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_attendance_marking(whatsapp_id, group_label, activity_date, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_attendance_marking (whatsapp_id, group_label, activity_date, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET group_label = EXCLUDED.group_label, activity_date = EXCLUDED.activity_date, created_at = EXCLUDED.created_at,
            reminded = FALSE
    """, (whatsapp_id, group_label, activity_date, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_attendance_marking(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_attendance_marking WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()


def mark_reminded(whatsapp_id):
    """Atomic: returns the pending row the FIRST time only (then it's marked), None after -- so the reminder shows once."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE pending_attendance_marking SET reminded = TRUE
        WHERE whatsapp_id = %s AND NOT reminded
        RETURNING group_label, activity_date
    """, (whatsapp_id,))
    row = cursor.fetchone()
    conn.commit()
    cursor.close()
    conn.close()
    return row
