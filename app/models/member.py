"""
app/models/member.py

Stage 2: The Members table.

reg_number (school registration number) is the true unique identity --
it's permanent and school-assigned. whatsapp_id is a separate, nullable
field that just points to a member's currently-known phone number, so
a member changing numbers doesn't create a duplicate record or need a
manual admin fix.

This table also doubles as the organization's source of truth for
active membership status, which matters beyond the chatbot itself.
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reg_number TEXT UNIQUE NOT NULL,
            whatsapp_id TEXT UNIQUE,
            name TEXT,
            gender TEXT,
            year_of_study INTEGER,
            area TEXT,
            consent_given BOOLEAN DEFAULT 0,
            registered_at TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
