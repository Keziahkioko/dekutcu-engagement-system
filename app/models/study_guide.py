"""
app/models/study_guide.py

Stage 14: study guides and their M-Pesa purchases (design settled with
Keziah 2026-09-30 -- see PROJECT_LOG.md).

  study_guides      One guide per semester, normally KES 70 unless the exec
                    subsidise it. Exactly ONE is "current" (on sale) --
                    enforced by a unique index, not just by the code.
                    Starting a new guide closes the previous one to new
                    purchases; nothing is ever deleted, so the history of
                    every guide stays (bought / collected per guide).
  guide_purchases   One row per payment attempt. status: pending (prompt
                    sent), paid, failed, cancelled. A member can have only
                    ONE paid purchase per guide, and an M-Pesa receipt code
                    can appear only once -- both unique indexes, so a
                    repeated Safaricom callback can't double-count.
                    collected_at/collected_by: the hand-over, confirmed by
                    the leader who gave them their copy.
  guide_batches     Printed copies the Discipleship team gives a group
                    leader (a batch in advance, top-ups when it runs out).
                    A leader's copies in hand = batches received - the
                    hand-overs they've confirmed.
  discipleship_team Subcommittee members the Discipleship Ministry Director
                    has added -- they, with the Director, record batches.
  pending_guide_creation  An exec leader part-way through "start a new
                    study guide".
"""

from app.database import get_connection

DEFAULT_PRICE_KES = 70


def init_study_guide_tables():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_guides (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            price_kes INTEGER NOT NULL CHECK (price_kes > 0),
            is_current BOOLEAN NOT NULL DEFAULT FALSE,
            started_by_reg_number TEXT,
            started_at TIMESTAMP NOT NULL,
            closed_at TIMESTAMP
        )
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_current_study_guide ON study_guides (is_current) WHERE is_current")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guide_purchases (
            id SERIAL PRIMARY KEY,
            guide_id INTEGER NOT NULL REFERENCES study_guides(id),
            reg_number TEXT NOT NULL,
            amount_kes INTEGER NOT NULL,
            phone TEXT,
            status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'failed', 'cancelled')),
            checkout_request_id TEXT UNIQUE,
            merchant_request_id TEXT,
            mpesa_receipt TEXT UNIQUE,
            result_code INTEGER,
            result_desc TEXT,
            requested_at TIMESTAMP NOT NULL,
            paid_at TIMESTAMP,
            collected_at TIMESTAMP,
            collected_by_reg_number TEXT
        )
    """)
    cursor.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_paid_purchase_per_guide
                      ON guide_purchases (guide_id, reg_number) WHERE status = 'paid'""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guide_batches (
            id SERIAL PRIMARY KEY,
            guide_id INTEGER NOT NULL REFERENCES study_guides(id),
            leader_reg_number TEXT NOT NULL,
            copies INTEGER NOT NULL CHECK (copies > 0),
            given_by_reg_number TEXT,
            given_at TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discipleship_team (
            reg_number TEXT PRIMARY KEY,
            added_by_reg_number TEXT,
            added_at TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_guide_creation (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            title TEXT,
            price_kes INTEGER,
            created_at TIMESTAMP NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def _one(sql, params=()):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone() if cursor.description else None
    conn.commit()
    cursor.close()
    conn.close()
    return row


def get_current_guide():
    return _one("SELECT * FROM study_guides WHERE is_current")


def start_new_guide(title, price_kes, started_by_reg_number, at):
    """
    Closes the current guide (if any) and makes a new one current -- in ONE
    transaction, so there's never a moment with two current guides or none.
    Returns (new_guide, previous_guide_or_None).
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE study_guides SET is_current = FALSE, closed_at = %s WHERE is_current RETURNING *", (at,))
        previous = cursor.fetchone()
        cursor.execute("""
            INSERT INTO study_guides (title, price_kes, is_current, started_by_reg_number, started_at)
            VALUES (%s, %s, TRUE, %s, %s) RETURNING *
        """, (title, price_kes, started_by_reg_number, at))
        new = cursor.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
    return new, previous


def count_paid_uncollected(guide_id):
    return _one("""SELECT COUNT(*) AS n FROM guide_purchases
                   WHERE guide_id = %s AND status = 'paid' AND collected_at IS NULL""", (guide_id,))["n"]


# --- pending "start a new study guide" conversation ---

def get_pending_guide_creation(whatsapp_id):
    return _one("SELECT * FROM pending_guide_creation WHERE whatsapp_id = %s", (whatsapp_id,))


def start_pending_guide_creation(whatsapp_id, at):
    _one("""
        INSERT INTO pending_guide_creation (whatsapp_id, step, created_at) VALUES (%s, 'awaiting_title', %s)
        ON CONFLICT (whatsapp_id) DO UPDATE SET step = 'awaiting_title', title = NULL, price_kes = NULL,
            created_at = EXCLUDED.created_at
    """, (whatsapp_id, at))


def update_pending_guide_creation(whatsapp_id, step, title=None, price_kes=None):
    _one("""UPDATE pending_guide_creation SET step = %s, title = COALESCE(%s, title), price_kes = COALESCE(%s, price_kes)
            WHERE whatsapp_id = %s""", (step, title, price_kes, whatsapp_id))


def delete_pending_guide_creation(whatsapp_id):
    _one("DELETE FROM pending_guide_creation WHERE whatsapp_id = %s", (whatsapp_id,))
