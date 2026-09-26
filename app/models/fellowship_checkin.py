"""
app/models/fellowship_checkin.py

Stage 7: the raw log of positive open-fellowship check-ins (Monday/
Wednesday/Thursday/Friday) -- one row per person per day they
confirmed attending. This is the data "regular attendee" status gets
inferred from (see app/services/fellowship_checkin.py), NOT a
pre-registered roster -- these are open gatherings with no fixed list
of expected attendees.
"""

from app.database import get_connection


def init_fellowship_checkins_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fellowship_checkins (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            checkin_date DATE NOT NULL,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def record_checkin(reg_number, activity_type, checkin_date, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO fellowship_checkins (reg_number, activity_type, checkin_date, created_at)
        VALUES (%s, %s, %s, %s)
    """, (reg_number, activity_type, checkin_date, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def count_checkins_on_dates(reg_number, activity_type, dates):
    """
    How many of the given specific calendar dates this member has a
    check-in row for -- NOT just "their last N check-ins ever", which
    would be wrong if they have gaps. The caller computes the actual
    last N calendar occurrences of the relevant weekday itself (pure
    date arithmetic, see fellowship_checkin.py), so this naturally
    handles cold start correctly: early on, most of those dates
    predate the feature even existing, and nobody has a row for them.
    """
    if not dates:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) AS n FROM fellowship_checkins
        WHERE reg_number = %s AND activity_type = %s AND checkin_date = ANY(%s)
    """, (reg_number, activity_type, dates))
    count = cursor.fetchone()["n"]
    cursor.close()
    conn.close()
    return count
