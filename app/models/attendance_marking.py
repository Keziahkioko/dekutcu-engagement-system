"""
app/models/attendance_marking.py

Stage 13: one row per Bible Study attendance marking by a group leader.
Added because reporting found a real gap: absences were stored, but a
"none" reply (everyone attended) left no trace at all -- so "the leader
marked and everyone came" was indistinguishable from "the leader never
marked", and any attendance RATE would have been wrong. roster_size and
absent_count are snapshotted at marking time, so later roster changes
don't rewrite history.
"""

from app.database import get_connection


def init_attendance_markings_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance_markings (
            id SERIAL PRIMARY KEY,
            group_label TEXT NOT NULL,
            activity_date DATE NOT NULL,
            roster_size INTEGER NOT NULL,
            absent_count INTEGER NOT NULL,
            marked_by_reg_number TEXT,
            marked_at TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def record_marking(group_label, activity_date, roster_size, absent_count, marked_by_reg_number, marked_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO attendance_markings (group_label, activity_date, roster_size, absent_count, marked_by_reg_number, marked_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (group_label, activity_date, roster_size, absent_count, marked_by_reg_number, marked_at))
    conn.commit()
    cursor.close()
    conn.close()
