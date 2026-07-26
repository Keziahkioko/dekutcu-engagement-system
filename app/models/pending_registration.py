"""
app/models/pending_registration.py

Stage 3: Scratch space for registrations in progress.

Registration is a multi-step WhatsApp conversation (reg number -> name
-> gender -> year -> area -> consent 1 -> consent 2 -> confirm), and
messages arrive one at a time. This table remembers "where" each
person is in that conversation, so progress survives a server
restart (Render can restart the app mid-conversation) instead of
living only in memory and vanishing.

Once registration is fully confirmed, the row's data is copied into
the permanent members table and this row is deleted -- it's staging,
not permanent storage.

Runs on PostgreSQL (see app/database.py). SQL placeholders use %s
(psycopg2 style), not sqlite3's ?.
"""

from app.database import get_connection

# The ordered steps of the registration conversation.
STEPS = [
    "awaiting_reg_number",
    "awaiting_name",
    "awaiting_gender",
    "awaiting_year_of_study",
    "awaiting_area",
    "awaiting_data_consent",
    "awaiting_followup_consent",
    "awaiting_confirmation",
]


def init_pending_registrations_table():
    """
    Creates the pending_registrations table if it does not already
    exist. Safe to call every time the app starts.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_registrations (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            reg_number TEXT,
            name TEXT,
            gender TEXT,
            year_of_study INTEGER,
            area TEXT,
            data_consent BOOLEAN,
            followup_consent BOOLEAN,
            started_at TIMESTAMP
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_pending_registration(whatsapp_id):
    """
    Returns the in-progress registration row for this whatsapp_id,
    or None if they don't have one.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_registrations WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_registration(whatsapp_id, started_at):
    """
    Creates a new pending registration row at the very first step.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_registrations (whatsapp_id, step, started_at)
        VALUES (%s, %s, %s)
    """, (whatsapp_id, STEPS[0], started_at))
    conn.commit()
    cursor.close()
    conn.close()


def update_pending_registration(whatsapp_id, **fields):
    """
    Updates one or more fields on an existing pending registration
    row, e.g. update_pending_registration(wid, step="awaiting_name",
    reg_number="C026-01-1234/2022").
    """
    if not fields:
        return

    conn = get_connection()
    cursor = conn.cursor()

    set_clause = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [whatsapp_id]

    cursor.execute(
        f"UPDATE pending_registrations SET {set_clause} WHERE whatsapp_id = %s",
        values
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_registration(whatsapp_id):
    """
    Removes a pending registration row -- called either when
    registration completes successfully (data has been copied into
    the real members table) or when the member cancels.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_registrations WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
