"""
app/models/pending_reassignment_resolution.py

Scratch space for a leader's in-progress "resolve a pending
reassignment" conversation -- pick which member's reassignment to
resolve, then pick which group to actually place them in (the
recommended one, or an override). Mirrors pending_leader_nominations'
shape (one row per whatsapp_id, a step column).
"""

from app.database import get_connection


def init_pending_reassignment_resolutions_table():
    """Creates the table if it does not already exist. Safe to call every time the app starts."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_reassignment_resolutions (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            member_reg_number TEXT,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_reassignment_resolution(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_reassignment_resolutions WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_reassignment_resolution(whatsapp_id, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_reassignment_resolutions (whatsapp_id, step, created_at)
        VALUES (%s, 'choosing_member', %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET step = 'choosing_member', member_reg_number = NULL, created_at = EXCLUDED.created_at
    """, (whatsapp_id, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def update_pending_reassignment_resolution(whatsapp_id, **fields):
    if not fields:
        return
    conn = get_connection()
    cursor = conn.cursor()
    set_clause = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [whatsapp_id]
    cursor.execute(
        f"UPDATE pending_reassignment_resolutions SET {set_clause} WHERE whatsapp_id = %s",
        values
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_reassignment_resolution(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_reassignment_resolutions WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
