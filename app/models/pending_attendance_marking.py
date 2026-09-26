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
        SET group_label = EXCLUDED.group_label, activity_date = EXCLUDED.activity_date, created_at = EXCLUDED.created_at
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
