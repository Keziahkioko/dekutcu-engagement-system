"""
app/models/pending_event_creation.py

Stage 6: Scratch space for a leader's in-progress "create an event"
conversation -- type, then title, date, time, location, description,
then a final confirm. Mirrors pending_leader_nomination.py's shape
(one row per whatsapp_id, a step column, fields filled in as the
conversation progresses).
"""

from app.database import get_connection


def init_pending_event_creation_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_event_creation (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            event_type TEXT,
            title TEXT,
            event_date TEXT,
            event_time TEXT,
            location TEXT,
            description TEXT,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_event_creation(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_event_creation WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_event_creation(whatsapp_id, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_event_creation (whatsapp_id, step, created_at)
        VALUES (%s, 'awaiting_type', %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET step = 'awaiting_type', event_type = NULL, title = NULL,
            event_date = NULL, event_time = NULL, location = NULL,
            description = NULL, created_at = EXCLUDED.created_at
    """, (whatsapp_id, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def update_pending_event_creation(whatsapp_id, **fields):
    if not fields:
        return
    conn = get_connection()
    cursor = conn.cursor()
    set_clause = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [whatsapp_id]
    cursor.execute(
        f"UPDATE pending_event_creation SET {set_clause} WHERE whatsapp_id = %s",
        values
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_event_creation(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_event_creation WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
