"""
app/models/pending_leader_nomination.py

Stage 6: Scratch space for an exec leader's in-progress "nominate a
group leader" conversation -- picking an area, then a candidate, then
whether to bypass on-platform confirmation. Mirrors
pending_registration.py's shape (one row per whatsapp_id, a step
column, fields filled in as the conversation progresses), but this
tracks the NOMINATING leader's side of the flow.

This is a separate concern from pending_actions: once a candidate is
actually asked to confirm, THEIR reply (accept/decline) is handled
through the existing pending_actions mechanism in intent_router.py,
not this table -- that's a simple one-shot YES/NO, not a multi-step
conversation, so it reuses infrastructure that already exists rather
than duplicating it here.

candidate_scope holds the area whose roster candidates are currently
being drawn from -- starts equal to `area` (the target area being
recruited for), but can be pointed at a DIFFERENT area if the exec
leader replies "other" (e.g. recruiting for Kahawa but drawing the
candidate from Internal Hostels instead). Deliberately never shows
every registered member in one message -- at real membership size
that overflows WhatsApp's ~4096-character message limit.
"""

from app.database import get_connection


def init_pending_leader_nominations_table():
    """
    Creates the pending_leader_nominations table if it does not
    already exist. Safe to call every time the app starts.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_leader_nominations (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            area TEXT,
            candidate_scope TEXT,
            candidate_reg_number TEXT,
            created_at TIMESTAMP
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_pending_leader_nomination(whatsapp_id):
    """Returns the in-progress nomination row for this whatsapp_id, or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_leader_nominations WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_leader_nomination(whatsapp_id, created_at):
    """
    Starts (or restarts, if one's already in progress -- a new attempt
    supersedes an old, presumably abandoned one) a nomination
    conversation at the first step.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_leader_nominations (whatsapp_id, step, created_at)
        VALUES (%s, 'awaiting_area', %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET step = 'awaiting_area', area = NULL, candidate_scope = 'area',
            candidate_reg_number = NULL, created_at = EXCLUDED.created_at
    """, (whatsapp_id, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def update_pending_leader_nomination(whatsapp_id, **fields):
    """
    Updates one or more fields, e.g.
    update_pending_leader_nomination(wid, step="awaiting_candidate", area="Bomas").
    """
    if not fields:
        return

    conn = get_connection()
    cursor = conn.cursor()

    set_clause = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [whatsapp_id]

    cursor.execute(
        f"UPDATE pending_leader_nominations SET {set_clause} WHERE whatsapp_id = %s",
        values
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_leader_nomination(whatsapp_id):
    """Removes a pending nomination row -- called on completion or cancellation."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_leader_nominations WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
