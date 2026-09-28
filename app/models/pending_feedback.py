"""
app/models/pending_feedback.py

Feedback collection: an outstanding "how was it?" question, same
one-row-per-whatsapp_id shape as every other pending_* table. Points at
the specific feedback_requests row by ID so the answer lands on exactly
that request. created_at is what the expiry check in feedback.py uses
-- see there for why these expire much faster than other pending flows.
"""

from app.database import get_connection


def init_pending_feedback_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_feedback (
            whatsapp_id TEXT PRIMARY KEY,
            feedback_request_id INTEGER NOT NULL REFERENCES feedback_requests(id),
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_feedback(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM pending_feedback WHERE whatsapp_id = %s", (whatsapp_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_feedback(whatsapp_id, feedback_request_id, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_feedback (whatsapp_id, feedback_request_id, created_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET feedback_request_id = EXCLUDED.feedback_request_id, created_at = EXCLUDED.created_at
    """, (whatsapp_id, feedback_request_id, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_feedback(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_feedback WHERE whatsapp_id = %s", (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()
