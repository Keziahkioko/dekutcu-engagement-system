"""
app/models/pending_escalation_consent.py

Stage 11: tracks an outstanding "would it be okay if I let a leader
know?" question -- ONLY used for general distress, never for acute
risk (which escalates immediately regardless of consent -- see
app/services/escalation.py for the reasoning). context_text and
trigger_type are carried through so, if the member says yes, the
actual escalation has real content to notify the leader with.
"""

from app.database import get_connection


def init_pending_escalation_consent_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_escalation_consent (
            whatsapp_id TEXT PRIMARY KEY,
            trigger_type TEXT NOT NULL,
            context_text TEXT,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_escalation_consent(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM pending_escalation_consent WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_pending_escalation_consent(whatsapp_id, trigger_type, context_text, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_escalation_consent (whatsapp_id, trigger_type, context_text, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET trigger_type = EXCLUDED.trigger_type, context_text = EXCLUDED.context_text, created_at = EXCLUDED.created_at
    """, (whatsapp_id, trigger_type, context_text, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_escalation_consent(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM pending_escalation_consent WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
