"""
app/models/pending_outbound.py

WhatsApp templates step 2 (Keziah, 2026-10-16): a message the bot wanted to
send OUTSIDE someone's 24-hour window (app/models/last_inbound.py) can't go
as an ordinary message. It's saved here, the person gets one short "you have
a new message, reply to read it" template, and when they reply the saved
messages are delivered (step 3) -- free, since their reply reopens the window.

Safeguards:
  - `notified`: once a template has gone out for this person's waiting
    messages, more messages just join the queue -- ONE template, not one per
    message (no spam, and each template costs ~KES 0.52);
  - kept 7 days only: a week-old "event tonight" is no use to anyone.
"""

from app.database import get_connection

KEEP_DAYS = 7


def init_pending_outbound_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_outbound (
            id SERIAL PRIMARY KEY,
            whatsapp_id TEXT NOT NULL,
            message_text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            notified BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS pending_outbound_whatsapp_id ON pending_outbound (whatsapp_id)")
    conn.commit()
    cursor.close()
    conn.close()


def save_outbound(whatsapp_id, message_text):
    """
    Saves one message to deliver when they next write. Returns True if they've ALREADY been sent a template
    for messages still waiting (so no new one is needed). Clears out anything older than a week first.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM pending_outbound WHERE created_at < NOW() - INTERVAL '{KEEP_DAYS} days'")
    cursor.execute("SELECT 1 FROM pending_outbound WHERE whatsapp_id = %s AND notified LIMIT 1", (whatsapp_id,))
    already_notified = cursor.fetchone() is not None
    cursor.execute("INSERT INTO pending_outbound (whatsapp_id, message_text, notified) VALUES (%s, %s, %s)",
                   (whatsapp_id, message_text, already_notified))
    conn.commit()
    cursor.close()
    conn.close()
    return already_notified


def mark_notified(whatsapp_id):
    """The template went out -- every message waiting for this person is covered by it."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE pending_outbound SET notified = TRUE WHERE whatsapp_id = %s", (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()
