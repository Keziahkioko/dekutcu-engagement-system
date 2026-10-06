"""
app/models/pending_reason_capture.py

Stage 7: tracks an outstanding "why were you absent?" question sent
directly to a member -- one row means that member has been asked and
hasn't replied yet. Points at the specific absences row via
absence_id, so the reply records against the exact right event with
no lookup ambiguity.
"""

from app.database import get_connection


def init_pending_reason_capture_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_reason_capture (
            whatsapp_id TEXT PRIMARY KEY,
            absence_id INTEGER NOT NULL REFERENCES absences(id),
            created_at TIMESTAMP
        )
    """)
    # 2026-10-07: TRUE when the absence was only inferred from silence (the noon sweep) -- then
    # "I was there" corrects it. Never for a leader's Bible Study marking.
    cursor.execute("ALTER TABLE pending_reason_capture ADD COLUMN IF NOT EXISTS from_silence BOOLEAN DEFAULT FALSE")
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_reason_capture(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_reason_capture WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_reason_capture(whatsapp_id, absence_id, created_at, from_silence=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_reason_capture (whatsapp_id, absence_id, created_at, from_silence)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET absence_id = EXCLUDED.absence_id, created_at = EXCLUDED.created_at, from_silence = EXCLUDED.from_silence
    """, (whatsapp_id, absence_id, created_at, from_silence))
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_reason_capture(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_reason_capture WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
