"""
app/models/pending_area_change.py

Scratch space for a member's in-progress "I'm updating my area" reply
-- just remembers we're waiting for their area-number answer, so the
next message is interpreted as answering that question rather than
reclassified by the LLM. Single-step (unlike pending_registrations or
pending_leader_nominations), so no "step" column is needed --
existence of a row IS the state.
"""

from app.database import get_connection


def init_pending_area_changes_table():
    """Creates the pending_area_changes table if it does not already exist. Safe to call every time the app starts."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_area_changes (
            whatsapp_id TEXT PRIMARY KEY,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_area_change(whatsapp_id):
    """Returns the pending row for this whatsapp_id, or None if they're not mid-area-change."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM pending_area_changes WHERE whatsapp_id = %s", (whatsapp_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_area_change(whatsapp_id, created_at):
    """Starts (or restarts) an area-change conversation -- a new attempt supersedes an old, presumably abandoned one."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_area_changes (whatsapp_id, created_at)
        VALUES (%s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE SET created_at = EXCLUDED.created_at
    """, (whatsapp_id, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_area_change(whatsapp_id):
    """Removes a pending area-change row -- called on completion or cancellation."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_area_changes WHERE whatsapp_id = %s", (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()
