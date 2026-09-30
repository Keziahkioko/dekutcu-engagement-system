"""
app/models/withdrawal.py

Consent withdrawal (settled with Keziah 2026-09-30 -- see PROJECT_LOG.md):
withdrawing ANONYMISES a member's records rather than just hiding them.
Their own words are deleted, everything else is detached from them with
an anonymous code, and their member record is deleted -- see
app/services/withdrawal.py for the whole procedure.

This module holds the two markers the rest of the app checks, so they're
defined once:
  - ANON_PREFIX: an anonymised row's reg_number/whatsapp_id is replaced
    with ANON_PREFIX + a random code. Nothing links that code back to
    the person. The reward jobs skip these rows (an anonymised member
    can never show up again, so "no new absence" would wrongly read as
    "came back").
  - REMOVED_TEXT: replaces a member's own words where the column can't
    be empty (companion questions, relayed questions). Reports skip it.

consent_withdrawals: one row per COMPLETED withdrawal, date only -- no
identity at all. Keeps "withdrew consent: N" in the membership report
true after the member records themselves are gone.
"""

from app.database import get_connection

ANON_PREFIX = "withdrawn-"
REMOVED_TEXT = "(removed -- the member withdrew consent)"


def init_consent_withdrawals_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consent_withdrawals (
            id SERIAL PRIMARY KEY,
            completed_at TIMESTAMP NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def count_withdrawals():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM consent_withdrawals")
    n = cursor.fetchone()["n"]
    cursor.close()
    conn.close()
    return n
