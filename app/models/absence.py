"""
app/models/absence.py

Stage 7: one row per captured absence -- deliberately generic
(activity_type as a plain column, not a Bible-Study-specific table)
so the open-fellowship side (not yet built) can write to the same
table later without a redesign, matching the shared-bandit design
settled in PROJECT_LOG.md. reason_category is the LLM classifier's
output (one of the six categories from the proposal); reason_raw is
the member's own words, kept alongside it.

distress_level (Stage 11) replaces the original shows_distress
boolean with a three-way severity ('none' / 'distress' / 'acute_risk')
-- the old column stays in the table for schema stability but is no
longer written to; there's no real production data on either yet, so
nothing is lost by the switch. See app/services/escalation.py for how
'distress' (asks consent first) and 'acute_risk' (escalates
regardless) are handled differently.

Keyed by reg_number, NOT whatsapp_id -- matching this project's own
identity model since Stage 2 (reg_number is the permanent identity;
whatsapp_id is a nullable, sometimes-absent pointer). An absence is
real, recordable data even for a member with no WhatsApp number on
file -- we just can't message them a reason-capture prompt in that
case, which app/services/attendance.py already guards separately.
"""

from datetime import timedelta

from app.database import get_connection
from app.models.withdrawal import ANON_PREFIX


def init_absences_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS absences (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            activity_date DATE NOT NULL,
            reason_raw TEXT,
            reason_category TEXT,
            shows_distress BOOLEAN,
            created_at TIMESTAMP
        )
    """)
    # Stage 8 additions -- which bandit arm was chosen for this absence
    # (and the exact context bucket it was sampled from, so a later
    # reward update applies to the SAME cell that was actually sampled,
    # not one recomputed after the fact with possibly-different data),
    # and the reward once it's known (NULL until the next occurrence
    # of that same activity has passed -- see bandit.py).
    cursor.execute("ALTER TABLE absences ADD COLUMN IF NOT EXISTS context_key TEXT")
    cursor.execute("ALTER TABLE absences ADD COLUMN IF NOT EXISTS chosen_arm TEXT")
    cursor.execute("ALTER TABLE absences ADD COLUMN IF NOT EXISTS reward BOOLEAN")
    # Stage 11 addition -- see module docstring for why this replaces
    # shows_distress rather than reusing it.
    cursor.execute("ALTER TABLE absences ADD COLUMN IF NOT EXISTS distress_level TEXT")
    # Stage 10 (fuller scope) additions -- is_control snapshots whether
    # this absence's member was in the bandit's static-reminder control
    # group AT SELECTION TIME (same "snapshot, don't recompute later"
    # principle as context_key/chosen_arm above). recovered_within_2 is
    # the proposal's own named evaluation metric (attendance recovery
    # within 2 sessions of a follow-up) -- a secondary, reporting-only
    # signal computed alongside `reward` but over a longer window,
    # never fed back into the live posterior update. See bandit.py.
    cursor.execute("ALTER TABLE absences ADD COLUMN IF NOT EXISTS is_control BOOLEAN")
    cursor.execute("ALTER TABLE absences ADD COLUMN IF NOT EXISTS recovered_within_2 BOOLEAN")
    conn.commit()
    cursor.close()
    conn.close()


def create_absence(reg_number, activity_type, activity_date, created_at):
    """
    Recorded the moment a leader marks someone absent (or, later, the
    moment an open-fellowship "regular" lapses) -- reason_raw/category/
    shows_distress start NULL and get filled in once/if the member
    actually replies to the reason-capture prompt.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO absences (reg_number, activity_type, activity_date, created_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (reg_number, activity_type, activity_date, created_at))
    absence_id = cursor.fetchone()["id"]
    conn.commit()
    cursor.close()
    conn.close()
    return absence_id


def get_absence_by_id(absence_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM absences WHERE id = %s", (absence_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def record_reason(absence_id, reason_raw, reason_category, distress_level):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE absences
        SET reason_raw = %s, reason_category = %s, distress_level = %s
        WHERE id = %s
    """, (reason_raw, reason_category, distress_level, absence_id))
    conn.commit()
    cursor.close()
    conn.close()


def count_consecutive_misses(reg_number, activity_type, up_to_date):
    """
    How many consecutive WEEKLY occurrences (ending at up_to_date,
    inclusive -- the current absence being processed already has its
    own row by the time this is called) this member has been absent
    for, counting backward until a gap -- a week with no absence row,
    meaning they attended and broke the streak.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT activity_date FROM absences
        WHERE reg_number = %s AND activity_type = %s AND activity_date <= %s
        ORDER BY activity_date DESC
    """, (reg_number, activity_type, up_to_date))
    dates = [row["activity_date"] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return weekly_streak(dates)


def weekly_streak(dates_newest_first):
    """
    The streak rule itself, with no database access -- shared by the
    bandit (via count_consecutive_misses, one absence at a time) and the
    lapsing-members report (which fetches every absence in one query and
    counts streaks here), so both always apply exactly the same rule.
    Counts back from the newest date while each earlier date is exactly
    one week before the last; any gap ends the streak.
    """
    if not dates_newest_first:
        return 0
    count = 1
    expected = dates_newest_first[0] - timedelta(days=7)
    for d in dates_newest_first[1:]:
        if d == expected:
            count += 1
            expected -= timedelta(days=7)
        else:
            break
    return count


def get_last_chosen_arm(reg_number, activity_type):
    """
    The most recent PRIOR arm chosen for this member+activity (if
    any) -- used for the bandit's recency constraint (never repeat the
    same arm twice running). Rows with chosen_arm still NULL (not yet
    decided) are naturally excluded by the WHERE clause.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT chosen_arm FROM absences
        WHERE reg_number = %s AND activity_type = %s AND chosen_arm IS NOT NULL
        ORDER BY activity_date DESC LIMIT 1
    """, (reg_number, activity_type))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row["chosen_arm"] if row else None


def set_chosen_arm(absence_id, context_key, chosen_arm, is_control):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE absences SET context_key = %s, chosen_arm = %s, is_control = %s WHERE id = %s",
        (context_key, chosen_arm, is_control, absence_id)
    )
    conn.commit()
    cursor.close()
    conn.close()


def set_reward(absence_id, reward):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE absences SET reward = %s WHERE id = %s", (reward, absence_id))
    conn.commit()
    cursor.close()
    conn.close()


def get_absences_awaiting_reward(cutoff_date):
    """
    Every absence with a chosen strategy but no reward yet, whose next weekly occurrence has already passed cutoff_date.
    Skips anonymised rows (a withdrawn member): they can never have another absence recorded, so "no new
    absence" would wrongly read as "came back" -- their outcome stays unknown instead.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM absences
        WHERE chosen_arm IS NOT NULL AND reward IS NULL AND activity_date <= %s
          AND reg_number NOT LIKE %s
    """, (cutoff_date, ANON_PREFIX + "%"))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_absences_awaiting_recovery(cutoff_date):
    """
    Every absence whose immediate reward is already known but whose
    recovered_within_2 (the proposal's own 2-session recovery metric)
    isn't yet -- i.e. whose SECOND weekly occurrence has already passed
    cutoff_date.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM absences
        WHERE reward IS NOT NULL AND recovered_within_2 IS NULL AND activity_date <= %s
          AND reg_number NOT LIKE %s
    """, (cutoff_date, ANON_PREFIX + "%"))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def set_recovered_within_2(absence_id, recovered):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE absences SET recovered_within_2 = %s WHERE id = %s", (recovered, absence_id))
    conn.commit()
    cursor.close()
    conn.close()


def absence_exists_for_date(reg_number, activity_type, activity_date):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1 FROM absences WHERE reg_number = %s AND activity_type = %s AND activity_date = %s
    """, (reg_number, activity_type, activity_date))
    exists = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    return exists
