"""
app/models/absence.py

Stage 7: one row per captured absence -- deliberately generic
(activity_type as a plain column, not a Bible-Study-specific table)
so the open-fellowship side (not yet built) can write to the same
table later without a redesign, matching the shared-bandit design
settled in PROJECT_LOG.md. reason_category is the LLM classifier's
output (one of the six categories from the proposal); reason_raw is
the member's own words, kept alongside it. shows_distress is a
safety-net flag checked at capture time -- Stage 11 (Escalation
Manager) doesn't exist yet, so this doesn't trigger anything
automated, it just makes sure a serious reply isn't filed away as an
ordinary data point (see app/services/attendance.py).

Keyed by reg_number, NOT whatsapp_id -- matching this project's own
identity model since Stage 2 (reg_number is the permanent identity;
whatsapp_id is a nullable, sometimes-absent pointer). An absence is
real, recordable data even for a member with no WhatsApp number on
file -- we just can't message them a reason-capture prompt in that
case, which app/services/attendance.py already guards separately.
"""

from app.database import get_connection


def init_absences_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS absences (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            activity_date DATE NOT NULL,
            reason_raw TEXT,
            reason_category TEXT,
            shows_distress BOOLEAN,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def create_absence(reg_number, activity_type, activity_date, created_at):
    """
    Recorded the moment a leader marks someone absent (or, later, the
    moment an open-fellowship "regular" lapses) -- reason_raw/category/
    shows_distress start NULL and get filled in once/if the member
    actually replies to the reason-capture prompt.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO absences (reg_number, activity_type, activity_date, created_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (reg_number, activity_type, activity_date, created_at))
    absence_id = cursor.fetchone()["id"]
    conn.commit()
    cursor.close()
    conn.close()
    return absence_id


def get_absence_by_id(absence_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM absences WHERE id = %s", (absence_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def record_reason(absence_id, reason_raw, reason_category, shows_distress):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE absences
        SET reason_raw = %s, reason_category = %s, shows_distress = %s
        WHERE id = %s
    """, (reason_raw, reason_category, shows_distress, absence_id))
    conn.commit()
    cursor.close()
    conn.close()
