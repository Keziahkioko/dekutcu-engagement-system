"""
app/models/pending_action.py

Stage 4: Scratch space for actions awaiting confirmation.

Several intents need a "are you sure?" step before actually doing
something (unsubscribe_followup, resume_followup, withdraw_data_
consent, and later send_announcement) -- rather than building a
one-off confirmation flow for each, this is one small reusable table:
it just remembers which action a member is currently being asked to
confirm, so their next YES/NO reply can be matched back to it.

This is intentionally much simpler than pending_registrations -- no
multi-step sequence, just one action name waiting for one yes/no.
"""

from datetime import datetime, timezone
from app.database import get_connection


def init_pending_actions_table():
    """
    Creates the pending_actions table if it does not already exist.
    Safe to call every time the app starts.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            whatsapp_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            created_at TIMESTAMP
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_pending_action(whatsapp_id):
    """
    Returns the pending action row for this whatsapp_id, or None if
    they don't have one awaiting confirmation.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_actions WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def set_pending_action(whatsapp_id, action):
    """
    Records that this member is now being asked to confirm `action`.
    Overwrites any existing pending action for them (a new request
    supersedes an old, presumably abandoned, one).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_actions (whatsapp_id, action, created_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET action = EXCLUDED.action, created_at = EXCLUDED.created_at
    """, (whatsapp_id, action, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    cursor.close()
    conn.close()


def clear_pending_action(whatsapp_id):
    """
    Removes a pending action -- called once it's been confirmed or
    declined.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_actions WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
