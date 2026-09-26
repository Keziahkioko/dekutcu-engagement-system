"""
app/models/bandit_posterior.py

Stage 8: one row per (context, arm) cell -- a Beta(alpha, beta)
posterior for tabular Thompson Sampling. Rows are created lazily
(default Beta(1,1), an uninformative prior) the first time a
particular context+arm combination is actually sampled, rather than
pre-seeding all cells up front.
"""

from app.database import get_connection


def init_bandit_posteriors_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bandit_posteriors (
            context_key TEXT NOT NULL,
            arm TEXT NOT NULL,
            alpha INTEGER NOT NULL DEFAULT 1,
            beta INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (context_key, arm)
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_posterior(context_key, arm):
    """Returns (alpha, beta), creating the row with the default Beta(1,1) prior if it doesn't exist yet."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bandit_posteriors (context_key, arm)
        VALUES (%s, %s)
        ON CONFLICT (context_key, arm) DO NOTHING
    """, (context_key, arm))
    conn.commit()
    cursor.execute(
        "SELECT alpha, beta FROM bandit_posteriors WHERE context_key = %s AND arm = %s",
        (context_key, arm)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row["alpha"], row["beta"]


def update_posterior(context_key, arm, reward):
    conn = get_connection()
    cursor = conn.cursor()
    if reward:
        cursor.execute("""
            INSERT INTO bandit_posteriors (context_key, arm, alpha, beta)
            VALUES (%s, %s, 2, 1)
            ON CONFLICT (context_key, arm) DO UPDATE SET alpha = bandit_posteriors.alpha + 1
        """, (context_key, arm))
    else:
        cursor.execute("""
            INSERT INTO bandit_posteriors (context_key, arm, alpha, beta)
            VALUES (%s, %s, 1, 2)
            ON CONFLICT (context_key, arm) DO UPDATE SET beta = bandit_posteriors.beta + 1
        """, (context_key, arm))
    conn.commit()
    cursor.close()
    conn.close()
