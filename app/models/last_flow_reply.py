"""
app/models/last_flow_reply.py

"Never loop forever" (Keziah, 2026-10-06): while a member is part-way through
a flow, the bot's LAST reply in it -- stored only as a fingerprint (a hash),
never the text. If the bot is about to send exactly the same reply again, the
member has given two unclear answers in a row, and the flow is stopped instead
(app/routes/webhook.py, _stop_if_repeating).
"""

import hashlib

from app.database import get_connection


def init_last_flow_reply_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS last_flow_reply (
            whatsapp_id TEXT PRIMARY KEY,
            flow TEXT NOT NULL,
            reply_hash TEXT NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def fingerprint(reply):
    return hashlib.sha256((reply or "").strip().encode("utf-8")).hexdigest()


def get_last_flow_reply(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM last_flow_reply WHERE whatsapp_id = %s", (whatsapp_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def set_last_flow_reply(whatsapp_id, flow, reply):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO last_flow_reply (whatsapp_id, flow, reply_hash) VALUES (%s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE SET flow = EXCLUDED.flow, reply_hash = EXCLUDED.reply_hash
    """, (whatsapp_id, flow, fingerprint(reply)))
    conn.commit()
    cursor.close()
    conn.close()


def clear_last_flow_reply(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM last_flow_reply WHERE whatsapp_id = %s", (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()
