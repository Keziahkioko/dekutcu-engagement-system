"""
app/models/last_inbound.py

WhatsApp templates (Keziah, 2026-10-15): Meta lets the bot send an ordinary
(free) message only within 24 hours of the person's LAST message to us --
the "customer service window". Outside it, only an approved template may be
sent. So for every WhatsApp number, the time of their last incoming message.

Its own table, not a column on members: unregistered people and leaders
message the bot too, and pending_messages can't be used (rows are deleted
once handled). Set in app/routes/webhook.py, _accept_message, for EVERY real
message -- images and voice notes too: the bot can't read them, but they
still open the window.
"""

from datetime import datetime, timedelta, timezone

from app.database import get_connection

# Meta's window is 24 hours; 23 leaves a margin, so a message sent near the end can't arrive after it has closed
# (Meta would reject it, and the member would get nothing).
WINDOW_HOURS = 23


def init_last_inbound_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS last_inbound (
            whatsapp_id TEXT PRIMARY KEY,
            received_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def record_inbound(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO last_inbound (whatsapp_id, received_at) VALUES (%s, NOW())
        ON CONFLICT (whatsapp_id) DO UPDATE SET received_at = EXCLUDED.received_at
    """, (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()


def get_last_inbound(whatsapp_id):
    """The time of their last message to us, or None if they've never written."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT received_at FROM last_inbound WHERE whatsapp_id = %s", (whatsapp_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row["received_at"] if row else None


def is_within_window(received_at, now=None):
    """The rule on its own (no database), so it can be tested with made-up times."""
    if received_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - received_at < timedelta(hours=WINDOW_HOURS)


def within_window(whatsapp_id):
    """True if an ordinary message to this number is allowed (and free) right now."""
    return is_within_window(get_last_inbound(whatsapp_id))
