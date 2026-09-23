"""
app/models/member.py

Stage 2/3: The Members table.

reg_number (school registration number) is the true unique identity --
it's permanent and school-assigned. whatsapp_id is a separate, nullable
field that just points to a member's currently-known phone number, so
a member changing numbers doesn't create a duplicate record or need a
manual admin fix.

This table also doubles as the organization's source of truth for
active membership status, which matters beyond the chatbot itself.

Consent is split into two separate fields, not one:
  - data_consent: storing details, group placement, event/RSVP
    communication, welfare-eligibility tracking.
  - followup_consent: the bot proactively checking in if the member
    starts missing sessions. Opting out of this does NOT affect
    active-membership/welfare-eligibility status -- that depends on
    actual attendance, not on follow-up opt-in.

is_leader gates the leader-only intents in intent_router.py (e.g.
allocate_groups, reshuffle_groups, leadership_query, send_announcement).
There's no self-service way to become a leader -- it's set manually in
the database for now.

group_label is the Bible study group a member is currently placed in
(e.g. "Bomas #2"), written by the Stage 5 allocation engine. NULL means
not yet placed -- either the allocation engine hasn't run for them yet,
or they were flagged for manual placement (area too small), or their
old placement was cleared (e.g. after withdrawing consent, or an area
change awaiting a leader's manual reassignment).

Stage 6 adds group-LEADER tracking (distinct from is_leader, which
gates exec/organizational actions -- a group leader runs one specific
area's Bible study, doesn't necessarily do allocation/reporting):
  - leader_of_area: the area they're a CONFIRMED group leader for.
    NULL if not currently a leader. Group leaders are recruited before
    allocation runs, so this is tied to an area, not a specific
    numbered group (which doesn't exist yet at recruitment time).
  - pending_leader_area / pending_leader_nominator: set while a
    nomination is awaiting the candidate's own YES/NO reply (see
    app/services/leader_assignment.py). pending_leader_nominator is
    the nominating exec leader's whatsapp_id, so the outcome (accept
    or decline) can be reported back to whoever asked.

Runs on PostgreSQL (see app/database.py). SQL placeholders use %s
(psycopg2 style), not sqlite3's ?.
"""

from app.database import get_connection


def init_members_table():
    """
    Creates the Members table if it does not already exist, and adds
    any columns introduced after the table's first creation. Both
    statements are idempotent -- safe to call every time the app
    starts, whether the table is brand new or already live.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id SERIAL PRIMARY KEY,
            reg_number TEXT UNIQUE NOT NULL,
            whatsapp_id TEXT UNIQUE,
            name TEXT,
            gender TEXT,
            year_of_study INTEGER,
            area TEXT,
            data_consent BOOLEAN DEFAULT FALSE,
            followup_consent BOOLEAN DEFAULT FALSE,
            registered_at TIMESTAMP
        )
    """)

    cursor.execute("""
        ALTER TABLE members
        ADD COLUMN IF NOT EXISTS is_leader BOOLEAN DEFAULT FALSE NOT NULL
    """)
    cursor.execute("""
        ALTER TABLE members
        ADD COLUMN IF NOT EXISTS group_label TEXT
    """)
    cursor.execute("""
        ALTER TABLE members
        ADD COLUMN IF NOT EXISTS leader_of_area TEXT
    """)
    cursor.execute("""
        ALTER TABLE members
        ADD COLUMN IF NOT EXISTS pending_leader_area TEXT
    """)
    cursor.execute("""
        ALTER TABLE members
        ADD COLUMN IF NOT EXISTS pending_leader_nominator TEXT
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_member_by_whatsapp_id(whatsapp_id):
    """
    Looks up a fully-registered member by their current WhatsApp ID.
    Returns the row (as a dict-like RealDictRow) or None if no match.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM members WHERE whatsapp_id = %s", (whatsapp_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def get_member_by_reg_number(reg_number):
    """
    Looks up a member by their reg_number -- used to recognize a
    returning member messaging from a new/different phone number.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM members WHERE reg_number = %s", (reg_number,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def create_member(reg_number, whatsapp_id, name, gender, year_of_study, area,
                   data_consent, followup_consent, registered_at):
    """
    Inserts a brand-new member row. Called once a pending registration
    is fully complete and confirmed.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO members (
            reg_number, whatsapp_id, name, gender, year_of_study, area,
            data_consent, followup_consent, registered_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (reg_number, whatsapp_id, name, gender, year_of_study, area,
          data_consent, followup_consent, registered_at))
    conn.commit()
    cursor.close()
    conn.close()


def relink_whatsapp_id(reg_number, new_whatsapp_id):
    """
    Updates an existing member's whatsapp_id -- used when a returning
    member messages from a new/different phone number. Their identity
    (reg_number) doesn't change; only the pointer to their current
    phone number does.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET whatsapp_id = %s WHERE reg_number = %s",
        (new_whatsapp_id, reg_number)
    )
    conn.commit()
    cursor.close()
    conn.close()


def get_data_consenting_members():
    """
    Returns every member with data_consent = TRUE -- the eligible pool
    for the Stage 5 allocation engine. Includes each member's current
    group_label (NULL if unplaced), so allocate_members_topup can tell
    who's already placed apart from who's new.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM members WHERE data_consent = TRUE")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def set_group_labels(updates):
    """
    Bulk-writes group_label for a batch of members, keyed by
    reg_number (the permanent identity -- not whatsapp_id, which can
    change). `updates` is a dict of {reg_number: group_label}.
    """
    conn = get_connection()
    cursor = conn.cursor()
    for reg_number, group_label in updates.items():
        cursor.execute(
            "UPDATE members SET group_label = %s WHERE reg_number = %s",
            (group_label, reg_number)
        )
    conn.commit()
    cursor.close()
    conn.close()


def count_group_placement_status():
    """
    Returns (placed_count, unplaced_count) among data-consenting
    members -- used to word the allocate_groups confirmation
    correctly (a first-ever run, where nobody is placed yet, needs
    different wording from a routine top-up).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            COUNT(*) FILTER (WHERE group_label IS NOT NULL) AS placed,
            COUNT(*) FILTER (WHERE group_label IS NULL) AS unplaced
        FROM members WHERE data_consent = TRUE
    """)
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row["placed"], row["unplaced"]


def get_group_summary():
    """
    Returns (group_counts, unplaced) for the view_groups leader
    report: group_counts is a list of (group_label, member_count)
    tuples sorted by label (which sorts groups within the same area
    together, since labels are "Area #N"); unplaced is how many
    data-consenting members have no group_label yet.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT group_label, COUNT(*) AS n
        FROM members
        WHERE data_consent = TRUE AND group_label IS NOT NULL
        GROUP BY group_label
        ORDER BY group_label
    """)
    group_counts = [(row["group_label"], row["n"]) for row in cursor.fetchall()]

    cursor.execute(
        "SELECT COUNT(*) AS n FROM members WHERE data_consent = TRUE AND group_label IS NULL"
    )
    unplaced = cursor.fetchone()["n"]

    cursor.close()
    conn.close()
    return group_counts, unplaced


def get_members_by_area(area):
    """Registered (data-consenting) members in one area, alphabetical -- used to build a candidate list."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM members WHERE area = %s AND data_consent = TRUE ORDER BY name",
        (area,)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def nominate_leader(reg_number, area, nominator_whatsapp_id):
    """
    Records that `reg_number` has been asked to lead `area`'s group,
    by the leader at `nominator_whatsapp_id`. Overwrites any existing
    pending nomination for them -- a new request supersedes an old,
    presumably abandoned one, same as set_pending_action.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET pending_leader_area = %s, pending_leader_nominator = %s WHERE reg_number = %s",
        (area, nominator_whatsapp_id, reg_number)
    )
    conn.commit()
    cursor.close()
    conn.close()


def assign_leader_directly(reg_number, area):
    """Bypass path: the exec leader already has the candidate's agreement -- confirm immediately, no reply needed."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET leader_of_area = %s, pending_leader_area = NULL, pending_leader_nominator = NULL WHERE reg_number = %s",
        (area, reg_number)
    )
    conn.commit()
    cursor.close()
    conn.close()


def confirm_leader_nomination(whatsapp_id):
    """Candidate accepted: promotes their pending_leader_area to leader_of_area."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE members
        SET leader_of_area = pending_leader_area,
            pending_leader_area = NULL,
            pending_leader_nominator = NULL
        WHERE whatsapp_id = %s
    """, (whatsapp_id,))
    conn.commit()
    cursor.close()
    conn.close()


def decline_leader_nomination(whatsapp_id):
    """Candidate declined: just clears the pending fields, leader_of_area untouched."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET pending_leader_area = NULL, pending_leader_nominator = NULL WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()


def remove_leader(reg_number):
    """Removes someone as a confirmed area leader (they quit, moved, etc.) -- the slot is open again."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE members SET leader_of_area = NULL WHERE reg_number = %s", (reg_number,))
    conn.commit()
    cursor.close()
    conn.close()


def get_leader_status():
    """
    Returns (confirmed, pending) for the view_group_leaders report:
    confirmed is a list of (area, name) for every leader_of_area
    that's set; pending is a list of (area, name) for every
    pending_leader_area that's set. Both ordered by area then name.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT leader_of_area AS area, name FROM members WHERE leader_of_area IS NOT NULL ORDER BY leader_of_area, name"
    )
    confirmed = [(row["area"], row["name"]) for row in cursor.fetchall()]

    cursor.execute(
        "SELECT pending_leader_area AS area, name FROM members WHERE pending_leader_area IS NOT NULL ORDER BY pending_leader_area, name"
    )
    pending = [(row["area"], row["name"]) for row in cursor.fetchall()]

    cursor.close()
    conn.close()
    return confirmed, pending


def get_pending_leader_nominees():
    """
    Returns (reg_number, name, area) for everyone with a pending leader
    nomination, ordered by area then name -- used to build the
    numbered "who accepted?" list for manually resolving a pending
    nomination outside their own YES/NO reply.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT reg_number, name, pending_leader_area AS area
        FROM members WHERE pending_leader_area IS NOT NULL
        ORDER BY pending_leader_area, name
    """)
    rows = [(row["reg_number"], row["name"], row["area"]) for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return rows


def get_confirmed_leaders():
    """Returns (reg_number, name, area) for every confirmed group leader -- used to build the "remove a leader" numbered list."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT reg_number, name, leader_of_area AS area
        FROM members WHERE leader_of_area IS NOT NULL
        ORDER BY leader_of_area, name
    """)
    rows = [(row["reg_number"], row["name"], row["area"]) for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return rows
