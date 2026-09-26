"""
app/models/pending_rsvp.py

Stage 6: Scratch space for a member's in-progress RSVP -- which
tracked event are they responding to (only needed if more than one is
open, via a numbered list, same pattern as every other leader/member
selection flow in this project), then their yes/no/maybe.
"""

from app.database import get_connection


def init_pending_rsvps_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_rsvps (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            event_id INTEGER,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_rsvp(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_rsvps WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_rsvp(whatsapp_id, step, created_at, event_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_rsvps (whatsapp_id, step, event_id, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET step = EXCLUDED.step, event_id = EXCLUDED.event_id, created_at = EXCLUDED.created_at
    """, (whatsapp_id, step, event_id, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def update_pending_rsvp(whatsapp_id, **fields):
    if not fields:
        return
    conn = get_connection()
    cursor = conn.cursor()
    set_clause = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [whatsapp_id]
    cursor.execute(
        f"UPDATE pending_rsvps SET {set_clause} WHERE whatsapp_id = %s",
        values
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_rsvp(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_rsvps WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
