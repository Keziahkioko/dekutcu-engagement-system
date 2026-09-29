"""
app/models/member_question.py

Stage 13: questions members ask inside their feedback that DeKUTCU's
materials couldn't answer (or that the member asked to go to a leader,
via ASK) -- relayed ANONYMOUSLY to the exec leaders, and the first
leader's answer relayed back by the bot. See
app/services/member_questions.py.

  - member_questions: the question, who asked (never shown to leaders --
    feedback is anonymous), and the answer once a leader gives one.
    Answering is atomic: two leaders replying at the same moment can't
    both "win", and the member gets exactly one answer.
  - pending_question_ask: the short-lived "reply ASK and I'll pass it to
    a leader" option after an automatic answer. Never insistent -- any
    other reply drops it -- and it expires.
"""

from app.database import get_connection


def init_member_question_tables():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS member_questions (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            question TEXT NOT NULL,
            feedback_request_id INTEGER,
            created_at TIMESTAMP,
            answered_by_reg_number TEXT,
            answer_text TEXT,
            answered_at TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_question_ask (
            whatsapp_id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            feedback_request_id INTEGER,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def _one(sql, params):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone()
    conn.commit()
    cursor.close()
    conn.close()
    return row


def create_question(reg_number, question, feedback_request_id, created_at):
    return _one("""
        INSERT INTO member_questions (reg_number, question, feedback_request_id, created_at)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (reg_number, question, feedback_request_id, created_at))["id"]


def get_question(question_id):
    return _one("SELECT * FROM member_questions WHERE id = %s", (question_id,))


def record_answer(question_id, leader_reg_number, answer_text, answered_at):
    """Atomic -- True only for the FIRST answer; later ones are refused."""
    return _one("""
        UPDATE member_questions SET answered_by_reg_number = %s, answer_text = %s, answered_at = %s
        WHERE id = %s AND answered_by_reg_number IS NULL RETURNING id
    """, (leader_reg_number, answer_text, answered_at, question_id)) is not None


def get_pending_question_ask(whatsapp_id):
    return _one("SELECT * FROM pending_question_ask WHERE whatsapp_id = %s", (whatsapp_id,))


def start_pending_question_ask(whatsapp_id, question, feedback_request_id, created_at):
    _one("""
        INSERT INTO pending_question_ask (whatsapp_id, question, feedback_request_id, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
        SET question = EXCLUDED.question, feedback_request_id = EXCLUDED.feedback_request_id,
            created_at = EXCLUDED.created_at
        RETURNING whatsapp_id
    """, (whatsapp_id, question, feedback_request_id, created_at))


def delete_pending_question_ask(whatsapp_id):
    _one("DELETE FROM pending_question_ask WHERE whatsapp_id = %s RETURNING whatsapp_id", (whatsapp_id,))
