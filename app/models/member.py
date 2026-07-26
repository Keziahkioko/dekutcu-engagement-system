"""
app/models/member.py

Stage 2/3: The Members table.

reg_number (school registration number) is the true unique identity --
it's permanent and school-assigned. whatsapp_id is a separate, nullable
field that just points to a member's currently-known phone number, so
a member changing numbers doesn't create a duplicate record or need a
manual admin fix.

This table also doubles as the organization's source of truth for
active membership status, which matters beyond the chatbot itself.

Consent is split into two separate fields, not one:
  - data_consent: storing details, group placement, event/RSVP
    communication, welfare-eligibility tracking.
  - followup_consent: the bot proactively checking in if the member
    starts missing sessions. Opting out of this does NOT affect
    active-membership/welfare-eligibility status -- that depends on
    actual attendance, not on follow-up opt-in.

Runs on PostgreSQL (see app/database.py). SQL placeholders use %s
(psycopg2 style), not sqlite3's ?.
"""

from app.database import get_connection


def init_members_table():
    """
    Creates the Members table if it does not already exist.
    Safe to call every time the app starts.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id SERIAL PRIMARY KEY,
            reg_number TEXT UNIQUE NOT NULL,
            whatsapp_id TEXT UNIQUE,
            name TEXT,
            gender TEXT,
            year_of_study INTEGER,
            area TEXT,
            data_consent BOOLEAN DEFAULT FALSE,
            followup_consent BOOLEAN DEFAULT FALSE,
            registered_at TIMESTAMP
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_member_by_whatsapp_id(whatsapp_id):
    """
    Looks up a fully-registered member by their current WhatsApp ID.
    Returns the row (as a dict-like RealDictRow) or None if no match.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM members WHERE whatsapp_id = %s", (whatsapp_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def get_member_by_reg_number(reg_number):
    """
    Looks up a member by their reg_number -- used to recognize a
    returning member messaging from a new/different phone number.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM members WHERE reg_number = %s", (reg_number,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def create_member(reg_number, whatsapp_id, name, gender, year_of_study, area,
                   data_consent, followup_consent, registered_at):
    """
    Inserts a brand-new member row. Called once a pending registration
    is fully complete and confirmed.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO members (
            reg_number, whatsapp_id, name, gender, year_of_study, area,
            data_consent, followup_consent, registered_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (reg_number, whatsapp_id, name, gender, year_of_study, area,
          data_consent, followup_consent, registered_at))
    conn.commit()
    cursor.close()
    conn.close()


def relink_whatsapp_id(reg_number, new_whatsapp_id):
    """
    Updates an existing member's whatsapp_id -- used when a returning
    member messages from a new/different phone number. Their identity
    (reg_number) doesn't change; only the pointer to their current
    phone number does.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET whatsapp_id = %s WHERE reg_number = %s",
        (new_whatsapp_id, reg_number)
    )
    conn.commit()
    cursor.close()
    conn.close()
