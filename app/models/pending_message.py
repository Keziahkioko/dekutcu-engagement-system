"""
app/models/pending_message.py

The durable, database-backed replacement for the in-memory message
queue webhook.py used to hold incoming WhatsApp messages in. An
in-memory queue is lost completely if the app process restarts (a
deploy, a crash) -- any messages still sitting in it at that moment
would silently never get a reply. This table survives a restart the
same way pending_registrations/pending_actions/etc. already do: a new
worker thread just picks up wherever the table left off.

Several worker threads (see webhook.py) each claim rows for their own
assigned SHARD of senders (sender_number hashed into a fixed number of
buckets), so any one sender's messages are only ever handled by one
worker and can never be processed out of order relative to each other
-- while different senders' messages can still be handled in parallel
across workers. claim_next_message uses SELECT ... FOR UPDATE SKIP
LOCKED specifically so two workers can safely poll the same table at
the same time without ever claiming the same row.
"""

from datetime import datetime, timezone
from app.database import get_connection


def init_pending_messages_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_messages (
            id SERIAL PRIMARY KEY,
            sender_number TEXT NOT NULL,
            message_text TEXT NOT NULL,
            received_at TIMESTAMP,
            claimed_at TIMESTAMP
        )
    """)
    # Meta's message IDs already accepted (QA 2026-10-01): Meta re-sends a
    # webhook it thinks failed, and the same message used to be processed --
    # and answered -- twice. Kept 7 days (see forget_old_message_ids).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            received_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def claim_message_id(message_id):
    """True the FIRST time a Meta message ID is seen; False for a repeat delivery (atomic)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING message_id",
                   (message_id,))
    first_time = cursor.fetchone() is not None
    conn.commit()
    cursor.close()
    conn.close()
    return first_time


def forget_old_message_ids():
    """Every scheduler check: Meta only re-sends within hours, so a week of IDs is plenty."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM processed_messages WHERE received_at < (NOW() AT TIME ZONE 'UTC') - INTERVAL '7 days'")
    conn.commit()
    cursor.close()
    conn.close()


def enqueue_message(sender_number, message_text):
    """
    Inserts the message and returns how many OTHER messages were
    already unclaimed at that moment (not counting this one) -- one
    connection for both the count and the insert, so the synchronous
    webhook handler (which decides whether to send a backlog filler
    reply from this return value) only pays for one DB round trip,
    not two. A fresh Postgres connection has real, measurable latency
    on its own -- doubling it up here quietly defeated part of the
    point of moving processing off the webhook's critical path.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM pending_messages WHERE claimed_at IS NULL")
    backlog_count = cursor.fetchone()["n"]
    cursor.execute("""
        INSERT INTO pending_messages (sender_number, message_text, received_at)
        VALUES (%s, %s, %s)
    """, (sender_number, message_text, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    cursor.close()
    conn.close()
    return backlog_count


def claim_next_message(shard_index, num_shards):
    """
    Atomically claims the oldest unclaimed message belonging to this
    worker's shard, or returns None if it doesn't have one waiting.
    Does NOT delete the row -- call delete_pending_message once the
    message has actually been handled (success or failure), so a
    worker that crashed mid-processing doesn't silently lose it.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, sender_number, message_text
        FROM pending_messages
        WHERE claimed_at IS NULL
          AND MOD(ABS(hashtext(sender_number)), %s) = %s
        ORDER BY id ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    """, (num_shards, shard_index))
    row = cursor.fetchone()

    if row is not None:
        cursor.execute(
            "UPDATE pending_messages SET claimed_at = %s WHERE id = %s",
            (datetime.now(timezone.utc).isoformat(), row["id"])
        )

    conn.commit()
    cursor.close()
    conn.close()
    return row


def delete_pending_message(message_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_messages WHERE id = %s", (message_id,))
    conn.commit()
    cursor.close()
    conn.close()
