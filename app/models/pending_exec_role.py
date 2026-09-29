"""
app/models/pending_exec_role.py

A leader part-way through setting exec offices (see
app/services/exec_roles.py) -- same one-row-per-whatsapp_id shape as
every other pending_* table. `step` is awaiting_office, awaiting_area or
awaiting_member; `office` and `area` carry the choices made so far.
"""

from app.database import get_connection


def init_pending_exec_role_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_exec_role (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            office TEXT,
            area TEXT,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_pending_exec_role(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM pending_exec_role WHERE whatsapp_id = %s", (whatsapp_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def set_pending_exec_role(whatsapp_id, step, office, area, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_exec_role (whatsapp_id, step, office, area, created_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET step = EXCLUDED.step, office = EXCLUDED.office, area = EXCLUDED.area, created_at = EXCLUDED.created_at
    """, (whatsapp_id, step, office, area, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def delete_pending_exec_role(whatsapp_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_exec_role WHERE whatsapp_id = %s", (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()
