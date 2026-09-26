"""
app/models/event_rsvp.py

Stage 6: One row per (event, member) RSVP -- "tracked" events only,
"broadcast" events don't have an RSVP concept at all.

Deliberately just RSVP response for now, NOT attendance confirmation --
that's Stage 7's concern (reason capture needs to know someone didn't
show before it makes sense to ask why). This table is built so Stage 7
can extend it later (e.g. an `attended` column) without a redesign,
not so it tries to anticipate that stage's work now.
"""

from app.database import get_connection


def init_event_rsvps_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS event_rsvps (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(id),
            whatsapp_id TEXT NOT NULL,
            response TEXT NOT NULL,
            responded_at TIMESTAMP,
            UNIQUE(event_id, whatsapp_id)
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def set_rsvp(event_id, whatsapp_id, response, responded_at):
    """
    A member can change their mind -- a repeat RSVP for the same event
    overwrites their previous response rather than erroring or
    duplicating.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO event_rsvps (event_id, whatsapp_id, response, responded_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (event_id, whatsapp_id) DO UPDATE
        SET response = EXCLUDED.response, responded_at = EXCLUDED.responded_at
    """, (event_id, whatsapp_id, response, responded_at))
    conn.commit()
    cursor.close()
    conn.close()
