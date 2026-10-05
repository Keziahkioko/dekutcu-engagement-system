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
  guide_batches     Printed copies the Guides Coordinator gives a group
                    leader (a batch in advance, top-ups when it runs out).
                    Two-sided because the guides are physical: a batch is
                    'pending' until the leader replies RECEIVED (with the
                    number they actually got); only confirmed copies count.
                    A leader's copies in hand = confirmed copies received -
                    the guides they've handed over to members.
  discipleship_team NO LONGER USED -- from the superseded subcommittee design
                    (replaced by one Guides Coordinator, 2026-10-05).
  pending_guide_creation  An exec leader part-way through "start a new
                    study guide".
  pending_guide_purchase  A member who's been asked which number to send the
                    M-Pesa prompt to (step 3).
"""

import psycopg2

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
    # Step 2 (2026-09-30): what the CALLBACK claimed, kept apart from the confirmed result -- a callback
    # isn't trusted until the status query agrees (see guide_payments.py). And 'duplicate': money really
    # taken a second time for a guide the member already paid for -- recorded so it's visible for a refund.
    cursor.execute("ALTER TABLE guide_purchases ADD COLUMN IF NOT EXISTS reported_result_code INTEGER")
    cursor.execute("ALTER TABLE guide_purchases ADD COLUMN IF NOT EXISTS reported_receipt TEXT")
    cursor.execute("ALTER TABLE guide_purchases DROP CONSTRAINT IF EXISTS guide_purchases_status_check")
    cursor.execute("""ALTER TABLE guide_purchases ADD CONSTRAINT guide_purchases_status_check
                      CHECK (status IN ('pending', 'paid', 'failed', 'cancelled', 'duplicate'))""")
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
    # Step 4 part 4 (2026-10-05): the member confirms they received their copy. receipt_status:
    # NULL (not handed over), 'awaiting' (handed over, member not yet answered -- "unconfirmed"),
    # 'confirmed', or 'disputed' (the member said NO: the hand-over was reversed).
    cursor.execute("ALTER TABLE guide_purchases ADD COLUMN IF NOT EXISTS receipt_status TEXT")
    cursor.execute("ALTER TABLE guide_purchases ADD COLUMN IF NOT EXISTS receipt_answered_at TIMESTAMP")
    cursor.execute("ALTER TABLE guide_purchases ADD COLUMN IF NOT EXISTS receipt_reminded BOOLEAN NOT NULL DEFAULT FALSE")
    # Step 4 part 2 (2026-10-05): the leader confirms receipt -- physical copies.
    cursor.execute("ALTER TABLE guide_batches ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'")
    cursor.execute("ALTER TABLE guide_batches ADD COLUMN IF NOT EXISTS copies_received INTEGER")
    cursor.execute("ALTER TABLE guide_batches ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP")
    cursor.execute("ALTER TABLE guide_batches ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMP")
    cursor.execute("ALTER TABLE guide_batches ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMP")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discipleship_team (
            reg_number TEXT PRIMARY KEY,
            added_by_reg_number TEXT,
            added_at TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_guide_purchase (
            whatsapp_id TEXT PRIMARY KEY,
            guide_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL
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


# --- purchases (Stage 14 step 2) ---

def create_pending_purchase(guide_id, reg_number, amount_kes, phone, at):
    return _one("""INSERT INTO guide_purchases (guide_id, reg_number, amount_kes, phone, status, requested_at)
                   VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING *""", (guide_id, reg_number, amount_kes, phone, at))


def attach_checkout(purchase_id, checkout_request_id, merchant_request_id):
    _one("UPDATE guide_purchases SET checkout_request_id = %s, merchant_request_id = %s WHERE id = %s",
         (checkout_request_id, merchant_request_id, purchase_id))


def get_purchase(purchase_id):
    return _one("SELECT * FROM guide_purchases WHERE id = %s", (purchase_id,))


def get_purchase_by_checkout(checkout_request_id):
    return _one("SELECT * FROM guide_purchases WHERE checkout_request_id = %s", (checkout_request_id,))


def record_reported(purchase_id, result_code, receipt):
    """What the callback CLAIMED -- stored, not trusted (the status query decides)."""
    _one("""UPDATE guide_purchases SET reported_result_code = %s, reported_receipt = COALESCE(%s, reported_receipt)
            WHERE id = %s AND status = 'pending'""", (result_code, receipt, purchase_id))


def settle_purchase(purchase_id, status, result_code, result_desc, receipt, at):
    """
    Pending -> final, ONCE: every update below only applies while the purchase is still pending,
    so a repeated callback (or the callback and the safety-net check both arriving) changes nothing.
    Returns the settled row, or None if it was already settled.

    Tried in order, each in its own transaction:
      1. the result as given;
      2. if the database refuses a 'paid' because the member ALREADY has a paid purchase of this
         guide -- money really taken twice -- record it as 'duplicate' so a refund can be arranged,
         rather than losing it;
      3. if even that is refused, the receipt code is already on ANOTHER purchase -- impossible for a
         genuine payment -- so it's kept as 'failed' with the reason, never as paid.
    """
    attempts = [
        ("""UPDATE guide_purchases SET status = %s, result_code = %s, result_desc = %s, mpesa_receipt = %s,
                   paid_at = CASE WHEN %s = 'paid' THEN %s ELSE NULL END
            WHERE id = %s AND status = 'pending' RETURNING *""",
         (status, result_code, result_desc, receipt, status, at, purchase_id)),
        ("""UPDATE guide_purchases SET status = 'duplicate', result_code = %s,
                   result_desc = 'paid twice for the same guide -- refund needed', mpesa_receipt = %s, paid_at = %s
            WHERE id = %s AND status = 'pending' RETURNING *""",
         (result_code, receipt, at, purchase_id)),
        ("""UPDATE guide_purchases SET status = 'failed', result_code = %s,
                   result_desc = 'receipt code already recorded on another purchase'
            WHERE id = %s AND status = 'pending' RETURNING *""",
         (result_code, purchase_id)),
    ]
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for sql, params in attempts:
            try:
                cursor.execute(sql, params)
                row = cursor.fetchone()
                conn.commit()
                return row
            except psycopg2.IntegrityError:
                conn.rollback()
        return None
    finally:
        cursor.close()
        conn.close()


def get_unsettled_purchases(requested_before):
    """Pending purchases older than the cutoff -- for the safety-net status check."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM guide_purchases WHERE status = 'pending' AND requested_at < %s ORDER BY requested_at",
                   (requested_before,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


# --- buying (Stage 14 step 3) ---

def get_paid_purchase(guide_id, reg_number):
    return _one("SELECT * FROM guide_purchases WHERE guide_id = %s AND reg_number = %s AND status = 'paid'",
                (guide_id, reg_number))


def get_recent_pending_purchase(guide_id, reg_number, since):
    return _one("""SELECT * FROM guide_purchases WHERE guide_id = %s AND reg_number = %s AND status = 'pending'
                   AND requested_at >= %s ORDER BY requested_at DESC LIMIT 1""", (guide_id, reg_number, since))


def get_guide(guide_id):
    return _one("SELECT * FROM study_guides WHERE id = %s", (guide_id,))


def get_pending_guide_purchase(whatsapp_id):
    return _one("SELECT * FROM pending_guide_purchase WHERE whatsapp_id = %s", (whatsapp_id,))


def start_pending_guide_purchase(whatsapp_id, guide_id, at):
    _one("""INSERT INTO pending_guide_purchase (whatsapp_id, guide_id, created_at) VALUES (%s, %s, %s)
            ON CONFLICT (whatsapp_id) DO UPDATE SET guide_id = EXCLUDED.guide_id, created_at = EXCLUDED.created_at""",
         (whatsapp_id, guide_id, at))


def delete_pending_guide_purchase(whatsapp_id):
    _one("DELETE FROM pending_guide_purchase WHERE whatsapp_id = %s", (whatsapp_id,))


def copies_in_hand(leader_reg_number, guide_id):
    """Confirmed copies this leader has received for the guide, minus the ones they've handed to members."""
    received = _one("""SELECT COALESCE(SUM(copies_received), 0) AS n FROM guide_batches
                       WHERE leader_reg_number = %s AND guide_id = %s AND status = 'confirmed'""",
                    (leader_reg_number, guide_id))["n"]
    handed = _one("""SELECT COUNT(*) AS n FROM guide_purchases
                     WHERE collected_by_reg_number = %s AND guide_id = %s AND collected_at IS NOT NULL""",
                  (leader_reg_number, guide_id))["n"]
    return int(received) - int(handed)


def get_group_leader(group_label):
    """The member currently leading this Bible Study group, or None."""
    if not group_label:
        return None
    return _one("SELECT * FROM members WHERE leads_group_label = %s LIMIT 1", (group_label,))
