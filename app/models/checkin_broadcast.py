"""
app/models/checkin_broadcast.py

Feedback collection: one row per "were you there?" check-in actually
sent out for a given activity on a given date -- whether a leader
triggered it live near the end of the session, or the 9pm schedule
sent it as a fallback. The UNIQUE constraint is what makes the 9pm
fallback safe: claim_checkin_broadcast only succeeds for whichever
trigger gets there first, so a leader-triggered check-in and the 9pm
one can never both go out for the same day, even if they race.

triggered_by also makes an evaluation comparison possible later:
response rates for in-the-room (leader) check-ins vs the 9pm ones.
"""

from app.database import get_connection


def init_checkin_broadcasts_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS checkin_broadcasts (
            id SERIAL PRIMARY KEY,
            activity_type TEXT NOT NULL,
            checkin_date DATE NOT NULL,
            triggered_by TEXT NOT NULL,
            sent_at TIMESTAMP,
            UNIQUE(activity_type, checkin_date)
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def claim_checkin_broadcast(activity_type, checkin_date, triggered_by, sent_at):
    """
    Returns True if this call claimed the day's check-in (nothing had
    been sent yet), False if one was already sent -- in which case the
    caller must NOT send another.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO checkin_broadcasts (activity_type, checkin_date, triggered_by, sent_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (activity_type, checkin_date) DO NOTHING
        RETURNING id
    """, (activity_type, checkin_date, triggered_by, sent_at))
    claimed = cursor.fetchone() is not None
    conn.commit()
    cursor.close()
    conn.close()
    return claimed


def get_checkin_broadcast(activity_type, checkin_date):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM checkin_broadcasts WHERE activity_type = %s AND checkin_date = %s",
        (activity_type, checkin_date)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row
