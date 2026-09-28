"""
app/models/feedback_request.py

Feedback collection: one row per feedback question actually sent to a
member, which doubles as the record of their answer. This shape is
deliberate -- the proposal evaluates feedback on RESPONSE RATE against
the old Google Forms baseline, so every request sent has to be counted,
not just the ones that got an answer:

    response rate = rows with responded_at set / all rows,
                    EXCLUDING trigger = 'unprompted'

`trigger` records where the request came from ('bible_study' after a
leader marks attendance, 'leader' for a leader-triggered check-in,
'scheduled' for the 9pm fallback), so response rate can be compared
per channel, not just overall. 'unprompted' rows are feedback a member
sent without being asked (activity_type 'general', sent_at NULL) --
kept so leaders see it, but never part of the response-rate figure,
since nobody asked, so it isn't a response.

A 'skip' reply sets responded_at but leaves response_text NULL -- the
member did reply (it counts toward response rate), they just declined
to give feedback. An expired or ignored request keeps responded_at
NULL. severity is the Stage 11 safety check's result on the reply.
"""

from app.database import get_connection


def init_feedback_requests_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback_requests (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            activity_date DATE NOT NULL,
            trigger TEXT NOT NULL,
            sent_at TIMESTAMP,
            responded_at TIMESTAMP,
            response_text TEXT,
            severity TEXT
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def create_feedback_request(reg_number, activity_type, activity_date, trigger, sent_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO feedback_requests (reg_number, activity_type, activity_date, trigger, sent_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (reg_number, activity_type, activity_date, trigger, sent_at))
    request_id = cursor.fetchone()["id"]
    conn.commit()
    cursor.close()
    conn.close()
    return request_id


def get_feedback_request_by_id(request_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM feedback_requests WHERE id = %s", (request_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def record_feedback_response(request_id, response_text, severity, responded_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE feedback_requests
        SET response_text = %s, severity = %s, responded_at = %s
        WHERE id = %s
    """, (response_text, severity, responded_at, request_id))
    conn.commit()
    cursor.close()
    conn.close()
