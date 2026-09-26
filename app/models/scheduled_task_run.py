"""
app/models/scheduled_task_run.py

Stage 7: guards a scheduled task (see app/services/scheduler.py)
against firing twice in the same day -- the scheduler thread checks
periodically (every few minutes) whether a task is due, so without
this guard a task whose window is an hour wide could fire multiple
times within it.
"""

from app.database import get_connection


def init_scheduled_task_runs_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_task_runs (
            task_name TEXT PRIMARY KEY,
            last_run_date DATE
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_last_run_date(task_name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT last_run_date FROM scheduled_task_runs WHERE task_name = %s",
        (task_name,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row["last_run_date"] if row else None


def mark_task_run(task_name, run_date):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO scheduled_task_runs (task_name, last_run_date)
        VALUES (%s, %s)
        ON CONFLICT (task_name) DO UPDATE SET last_run_date = EXCLUDED.last_run_date
    """, (task_name, run_date))
    conn.commit()
    cursor.close()
    conn.close()
