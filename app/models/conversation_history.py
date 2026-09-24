"""
app/models/conversation_history.py

A short, bounded rolling log of recent exchanges per whatsapp_id, so
the LLM (both intent classification and group_query's tool-calling)
has enough context to resolve short follow-ups ("then what am I",
"Internal means Internal Hostels") that make no sense read in
isolation. NOT a permanent chat archive -- callers only ever ask for
the last N messages (see get_recent_conversation), and it's cleared
entirely on withdraw_data_consent (see intent_router.py).

Deliberately scoped to only the general, freely-classified chat path
(app/services/intent_router.py's handle_message) -- the OTHER
conversational flows in this project (registration, pending-action
confirmations, leader nomination, area change) already have their own
dedicated step-tracking and don't go through ambiguous LLM
classification while mid-flow, so they don't need this.

Stores raw message text -- unlike every other table in this project,
which stores structured, extracted fields. Covered under the same
data_consent a member already gives (this is operational data in
service of the same conversation they consented to, not a new
purpose, never leader-visible), but worth naming explicitly since it's
a new category of data for this system.

Runs on PostgreSQL (see app/database.py). SQL placeholders use %s
(psycopg2 style), not sqlite3's ?.
"""

from app.database import get_connection


def init_conversation_history_table():
    """Creates the conversation_history table if it does not already exist. Safe to call every time the app starts."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_history (
            id SERIAL PRIMARY KEY,
            whatsapp_id TEXT NOT NULL,
            role TEXT NOT NULL,
            message_text TEXT NOT NULL,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def log_conversation_message(whatsapp_id, role, message_text):
    """role is 'user' or 'assistant'."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO conversation_history (whatsapp_id, role, message_text, created_at) VALUES (%s, %s, %s, NOW())",
        (whatsapp_id, role, message_text)
    )
    conn.commit()
    cursor.close()
    conn.close()


def get_recent_conversation(whatsapp_id, limit=10):
    """
    Returns the last `limit` messages for this whatsapp_id, in
    chronological order (oldest first) -- ready to drop straight into
    a Groq messages list ahead of the current message. Does NOT
    include whatever message is currently being processed -- callers
    log the current exchange AFTER handling it, not before, so this
    never doubles up with the message being classified/answered right now.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, message_text FROM conversation_history WHERE whatsapp_id = %s ORDER BY id DESC LIMIT %s",
        (whatsapp_id, limit)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return list(reversed(rows))


def clear_conversation_history(whatsapp_id):
    """Deletes all history for this whatsapp_id -- called on withdraw_data_consent, so withdrawing genuinely means no trace is kept."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM conversation_history WHERE whatsapp_id = %s", (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()
