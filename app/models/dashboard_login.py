"""
app/models/dashboard_login.py

Stage 13 step 4: one-time login codes for the reports website (see
app/routes/dashboard.py and PROJECT_LOG.md for the design Keziah agreed).

Only a HASH of each code is stored (SHA-256 -- fine here because the
codes are long and random, unlike passwords, so there's nothing to
brute-force): a copied database can't be turned back into working login
links. Each code is single-use (used_at) and expires 15 minutes after it
was issued. Claiming a code is atomic, so the same link opened twice at
the same moment still logs in only once.
"""

import hashlib

from app.database import get_connection


def hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def init_dashboard_login_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_login_codes (
            code_hash TEXT PRIMARY KEY,
            reg_number TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def store_code(code, reg_number, created_at, expires_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO dashboard_login_codes (code_hash, reg_number, created_at, expires_at)
        VALUES (%s, %s, %s, %s)
    """, (hash_code(code), reg_number, created_at, expires_at))
    conn.commit()
    cursor.close()
    conn.close()


def claim_code(code, now):
    """
    Marks the code used and returns whose it is -- or None if it doesn't
    exist, was already used, or has expired. One atomic UPDATE, so it can
    only ever succeed once.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE dashboard_login_codes SET used_at = %s
        WHERE code_hash = %s AND used_at IS NULL AND expires_at > %s
        RETURNING reg_number
    """, (now, hash_code(code), now))
    row = cursor.fetchone()
    conn.commit()
    cursor.close()
    conn.close()
    return row["reg_number"] if row else None
