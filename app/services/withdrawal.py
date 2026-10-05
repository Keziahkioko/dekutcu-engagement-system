"""
app/services/withdrawal.py

What happens when a member withdraws consent (settled with Keziah
2026-09-30 -- see PROJECT_LOG.md). The member is told, before they
confirm, that withdrawing "fully removes you from tracking -- your Bible
Study group placement, membership records, and anything tied to welfare
eligibility". Before this, the system only switched their consents off:
their own words, their history and their member record all stayed,
still linked to them, and the bot kept treating them as registered.

Chosen: ANONYMISE, not just hide and not delete outright.
  - Their own words are deleted (absence reasons, feedback, companion
    questions, relayed questions, escalation context).
  - Every row that stays gets an anonymous code in place of their reg
    number / WhatsApp number -- nothing links it back to them. Counts,
    charts and Objective 3 stay correct: those events really happened,
    they just no longer belong to anyone. (Deleting everything would
    change past figures whenever someone withdraws.)
  - Their member record is deleted, so a later message starts
    registration again -- which is what the bot has always told them.
  - Everything happens in ONE transaction: never half-anonymised.

Open escalation cases follow the two-tier model (Keziah's choice):
  - a NORMAL case was opened because the member agreed to a leader
    reaching out; withdrawing takes that back -- the case is closed and
    the leaders notified on it are told not to reach out;
  - an ACUTE case was opened regardless of consent, because of a safety
    concern; withdrawing doesn't remove that -- it stays open (reminders,
    backstop) until a leader claims it, and the member is told openly
    that a leader will still check in once. Anonymising waits until
    then; finish_pending_withdrawals (every scheduler check) completes it.
    The same task also completes withdrawals made before this existed.
"""

import secrets
from datetime import datetime, timezone

from app.database import get_connection
from app.models.escalation import close_case, get_open_cases_for_member, notified_leaders
from app.models.member import get_member_by_reg_number, get_member_by_whatsapp_id
from app.models.withdrawal import ANON_PREFIX, REMOVED_TEXT
from app.services.whatsapp_client import send_whatsapp_message

CLOSED_BECAUSE_WITHDREW = "member_withdrew"

# Half-finished conversations, all keyed by whatsapp_id -- deleted, or the
# member's next message would be read as a reply to one of them.
_PENDING_TABLES = [
    "pending_actions", "pending_area_changes", "pending_attendance_marking", "pending_escalation_consent",
    "pending_event_creation", "pending_exec_role", "pending_feedback", "pending_fellowship_checkin",
    "pending_leader_nominations", "pending_question_ask", "pending_reason_capture",
    "pending_reassignment_resolutions", "pending_registrations", "pending_rsvps", "pending_guide_creation",
    "pending_guide_purchase", "pending_announcement",
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def withdraw(whatsapp_id):
    """
    Called once the member has confirmed (YES) that they want to withdraw
    completely. Returns the reply to send them.
    """
    member = get_member_by_whatsapp_id(whatsapp_id)
    if not member:
        return "You're not registered, so there's nothing to withdraw."

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""UPDATE members SET data_consent = FALSE, followup_consent = FALSE, group_label = NULL
                      WHERE reg_number = %s""", (member["reg_number"],))
    conn.commit()
    cursor.close()
    conn.close()

    acute_open, cancelled = False, False
    for case in get_open_cases_for_member(member["reg_number"]):
        if case["urgency"] == "acute":
            acute_open = True
        elif close_case(case["id"], CLOSED_BECAUSE_WITHDREW, _now()):
            cancelled = True
            _tell_leaders_case_closed(case["id"])

    if not acute_open:
        anonymise(member)

    if acute_open:
        # Only say what has actually happened -- removal waits until a leader has checked in.
        reply = ("Your consent has been withdrawn and you won't get any more check-ins. Because of something you "
                 "shared earlier that worried me, a leader will still check in with you once -- after that, your "
                 "details, your history and the things you told me will be removed.")
    else:
        reply = ("Your consent has been withdrawn. I've removed your details, your group placement and "
                 "the things you told me, and what's left of your history is no longer linked to you.")
    if cancelled:
        reply += " Any request for a leader to contact you has been cancelled."
    return reply + " If you'd like to rejoin later, just message me again to re-register."


def _tell_leaders_case_closed(case_id):
    for reg_number in notified_leaders(case_id):
        leader = get_member_by_reg_number(reg_number)
        if leader and leader["whatsapp_id"]:
            send_whatsapp_message(
                leader["whatsapp_id"],
                f"Case {case_id} is closed: the member has withdrawn from the system, so please don't reach out about it.",
            )


def anonymise(member):
    """
    Deletes the member's own words and record, and detaches everything else from them.
    One transaction: if any step fails, nothing changes.
    """
    token = ANON_PREFIX + secrets.token_hex(6)
    reg, wa = member["reg_number"], member["whatsapp_id"]
    steps = [
        # Their own words go; the rest is kept under the anonymous code.
        ("UPDATE absences SET reg_number = %(t)s, reason_raw = NULL WHERE reg_number = %(r)s", None),
        ("UPDATE fellowship_checkins SET reg_number = %(t)s WHERE reg_number = %(r)s", None),
        ("UPDATE feedback_requests SET reg_number = %(t)s, response_text = NULL WHERE reg_number = %(r)s", None),
        ("UPDATE rag_queries SET reg_number = %(t)s, question = %(removed)s, answer = NULL WHERE reg_number = %(r)s", None),
        ("UPDATE member_questions SET reg_number = %(t)s, question = %(removed)s WHERE reg_number = %(r)s", None),
        ("UPDATE escalation_cases SET reg_number = %(t)s, context_text = NULL WHERE reg_number = %(r)s", None),
        ("UPDATE escalations SET reg_number = %(t)s, context_text = NULL WHERE reg_number = %(r)s", None),
        # Things they did as a leader, if they were one.
        ("UPDATE member_questions SET answered_by_reg_number = %(t)s WHERE answered_by_reg_number = %(r)s", None),
        ("UPDATE escalation_cases SET claimed_by_reg_number = %(t)s WHERE claimed_by_reg_number = %(r)s", None),
        ("UPDATE escalations SET notified_leader_reg_number = %(t)s WHERE notified_leader_reg_number = %(r)s", None),
        ("UPDATE attendance_markings SET marked_by_reg_number = %(t)s WHERE marked_by_reg_number = %(r)s", None),
        # Study guides (Stage 14): purchases kept for the sales history, but the phone number charged is theirs.
        ("UPDATE guide_purchases SET reg_number = %(t)s, phone = NULL WHERE reg_number = %(r)s", None),
        ("UPDATE guide_purchases SET collected_by_reg_number = %(t)s WHERE collected_by_reg_number = %(r)s", None),
        ("UPDATE guide_batches SET leader_reg_number = %(t)s WHERE leader_reg_number = %(r)s", None),
        ("UPDATE guide_batches SET given_by_reg_number = %(t)s WHERE given_by_reg_number = %(r)s", None),
        ("UPDATE study_guides SET started_by_reg_number = %(t)s WHERE started_by_reg_number = %(r)s", None),
        ("DELETE FROM discipleship_team WHERE reg_number = %(r)s", None),
        ("UPDATE announcements SET sent_by_reg_number = %(t)s WHERE sent_by_reg_number = %(r)s", None),
        # Number-change requests hold their phone numbers -- nothing worth keeping.
        ("DELETE FROM number_changes WHERE reg_number = %(r)s", None),
        ("UPDATE discipleship_team SET added_by_reg_number = %(t)s WHERE added_by_reg_number = %(r)s", None),
        ("DELETE FROM dashboard_login_codes WHERE reg_number = %(r)s", None),
        ("DELETE FROM pending_leader_nominations WHERE candidate_reg_number = %(r)s", None),
        ("DELETE FROM pending_reassignment_resolutions WHERE member_reg_number = %(r)s", None),
        # Keyed by WhatsApp number.
        ("UPDATE event_rsvps SET whatsapp_id = %(t)s WHERE whatsapp_id = %(w)s", "w"),
        ("UPDATE events SET created_by = %(t)s WHERE created_by = %(w)s", "w"),
        ("DELETE FROM conversation_history WHERE whatsapp_id = %(w)s", "w"),
    ] + [(f"DELETE FROM {table} WHERE whatsapp_id = %(w)s", "w") for table in _PENDING_TABLES] + [
        # Finally the member record itself, and an identity-free count.
        ("DELETE FROM members WHERE reg_number = %(r)s", None),
        ("INSERT INTO consent_withdrawals (completed_at) VALUES (%(now)s)", None),
    ]
    params = {"t": token, "r": reg, "w": wa, "removed": REMOVED_TEXT, "now": _now()}
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for sql, needs in steps:
            if needs == "w" and not wa:
                continue
            cursor.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def finish_pending_withdrawals():
    """
    Every scheduler check (safe to repeat). Completes any withdrawal still
    waiting: members with data_consent off and no open ACUTE case -- i.e.
    their acute case has now been claimed, or they withdrew before
    anonymising existed.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT m.* FROM members m
        WHERE NOT m.data_consent AND NOT EXISTS (
            SELECT 1 FROM escalation_cases c
            WHERE c.reg_number = m.reg_number AND c.urgency = 'acute'
              AND c.claimed_by_reg_number IS NULL AND c.closed_at IS NULL)
    """)
    waiting = cursor.fetchall()
    cursor.close()
    conn.close()
    for member in waiting:
        for case in get_open_cases_for_member(member["reg_number"]):   # a normal case opened before they withdrew
            if close_case(case["id"], CLOSED_BECAUSE_WITHDREW, _now()):
                _tell_leaders_case_closed(case["id"])
        anonymise(member)
