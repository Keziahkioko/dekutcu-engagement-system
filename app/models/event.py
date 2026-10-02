"""
app/models/event.py

Stage 6: Event & RSVP Manager.

Two event types, per the proposal's explicit scope split (Section 1.6):
  - "tracked" -- Bible Study sessions, cell group meetings, fellowship
    events. Individual members RSVP, and (in a LATER stage -- Stage 7)
    attendance gets confirmed and feeds the bandit engine. Organization
    -wide: every data-consenting member with a group placement, not
    scoped to one specific group/area (confirmed directly, not assumed).
  - "broadcast" -- large open gatherings (Sunday service) where
    "individual follow-up expectations do not apply". Announcement
    only -- no RSVP, no attendance tracking.

event_date is a real DATE column (not freeform text) specifically so
"what's coming up" can actually filter out past events in SQL --
event_time stays plain text since precise time-based sorting isn't
needed, just a real filterable date is.
"""

from app.database import get_connection


def init_events_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            event_date DATE NOT NULL,
            event_time TEXT,
            location TEXT,
            description TEXT,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def create_event(event_type, title, event_date, event_time, location, description, created_by, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO events (event_type, title, event_date, event_time, location, description, created_by, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (event_type, title, event_date, event_time, location, description, created_by, created_at))
    event_id = cursor.fetchone()["id"]
    conn.commit()
    cursor.close()
    conn.close()
    return event_id


def get_event_by_id(event_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def get_upcoming_events(event_type=None):
    """
    Every event from today onward, soonest first. Optionally filtered
    to one event_type ("tracked" or "broadcast"). Same-day events keep a
    FIXED order (by id): the RSVP list is fetched again when the member
    replies with a number, and without a tie-breaker "2" could point to a
    different event than the one shown (found in QA testing, 2026-10-01).
    """
    conn = get_connection()
    cursor = conn.cursor()
    if event_type:
        cursor.execute(
            "SELECT * FROM events WHERE event_date >= CURRENT_DATE AND event_type = %s ORDER BY event_date ASC, id ASC",
            (event_type,)
        )
    else:
        cursor.execute(
            "SELECT * FROM events WHERE event_date >= CURRENT_DATE ORDER BY event_date ASC, id ASC"
        )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows
