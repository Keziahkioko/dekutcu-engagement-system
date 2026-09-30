"""
app/services/attendance.py

Stage 7: the Bible Study side of reason capture -- the scheduled
Tuesday-evening leader nudge, and the leader's absence-marking reply.
Reason capture itself (classification, the distress safety-net) is
shared machinery -- see reason_capture.py -- since the open-fellowship
slice (fellowship_checkin.py) needs the exact same thing.

Feedback collection extends the marking reply: once a leader has said
who was absent, everyone else on the roster is known to have attended
(the same leader-confirmed attendance the bandit relies on), so they
get the feedback question -- see feedback.py.

Deliberately Bible-Study-only for now -- see PROJECT_LOG.md's
Objective 3 entry for how this and the open-fellowship slice share the
same `absences` table without sharing everything else.
"""

from datetime import datetime, timezone, date

from app.models.member import get_data_consenting_members, get_group_members, get_member_by_whatsapp_id
from app.models.attendance_marking import record_marking
from app.models.pending_attendance_marking import (
    get_pending_attendance_marking,
    start_pending_attendance_marking,
    delete_pending_attendance_marking,
    mark_reminded,
)
from app.models.absence import create_absence
from app.services.whatsapp_client import send_whatsapp_message
from app.services import reason_capture
from app.services import feedback

ACTIVITY_TYPE_BIBLE_STUDY = "bible_study"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return date.today()


def send_bible_study_nudges():
    """
    The scheduled Tuesday-9pm task (see scheduler.py) -- messages every
    leader currently matched to a specific Bible Study group, asking
    who was absent.
    """
    members = get_data_consenting_members()
    leaders = [m for m in members if m["leads_group_label"] and m["whatsapp_id"]]

    for leader in leaders:
        group_label = leader["leads_group_label"]
        roster = get_group_members(group_label)
        if not roster:
            continue

        lines = [f"{i + 1}. {m['name']}" for i, m in enumerate(roster)]
        message = (
            f"Who was absent from {group_label} Bible Study today?\n\n"
            + "\n".join(lines)
            + "\n\nReply with their numbers separated by commas (e.g. 2,5), or 'none' if everyone attended."
        )
        response = send_whatsapp_message(leader["whatsapp_id"], message)
        if response.status_code == 200:
            start_pending_attendance_marking(leader["whatsapp_id"], group_label, _today(), _now())


# Replies that mean "nobody was absent".
_NOBODY_ABSENT = {
    "none", "nobody", "no one", "noone", "nil", "none absent", "nobody absent", "no one absent",
    "everyone", "everyone came", "everyone attended", "everyone was there", "everyone was present",
    "all attended", "all came", "all present", "all were present", "all were there", "full attendance",
}
# Words a leader might wrap around the numbers: "2 and 5", "2, 5 were absent", "only 3".
_FILLER = {"and", "&", "were", "was", "absent", "missed", "only", "number", "numbers", "no", "no."}


def _parse_marking(text, roster_size):
    """
    Reads a leader's reply to "who was absent?". Returns:
      ("none", [])        -- nobody was absent
      ("absent", [2, 5])  -- these roster numbers were absent
      ("bad_numbers", []) -- clearly an attempt at numbers, but not valid ones (e.g. 12 in a group of 8)
      None                -- not an answer to the question at all (e.g. "I wanna see a report")
    """
    cleaned = text.strip().lower().rstrip(".!")
    if cleaned in _NOBODY_ABSENT:
        return ("none", [])
    tokens = [t for t in cleaned.replace(",", " ").replace("&", " & ").split() if t not in _FILLER]
    if not tokens or not all(t.isdigit() for t in tokens):
        return None
    numbers = sorted({int(t) for t in tokens})
    if not all(1 <= n <= roster_size for n in numbers):
        return ("bad_numbers", [])
    return ("absent", numbers)


def take_reminder(whatsapp_id):
    """
    The ONE gentle reminder, appended to the reply the first time a leader's
    message is routed elsewhere while their marking is still open. Empty
    string every time after that (and once the marking is done).
    """
    row = mark_reminded(whatsapp_id)
    if not row:
        return ""
    day = row["activity_date"].strftime("%A")
    return (f"\n\n(You haven't marked {row['group_label']}'s {day} attendance yet -- reply with the "
            "numbers of anyone absent, or 'none', whenever you're ready.)")


def handle_attendance_marking_message(whatsapp_id, message_text):
    """
    Returns the reply, or None if the message isn't an answer to "who was
    absent?" -- then it's routed normally (webhook.py) and the question stays
    open until answered or replaced by next Tuesday's nudge. Before this
    (fixed 2026-09-30, found in Keziah's live test) ANY other message was
    treated as a bad answer and got the roster list back, trapping the
    leader -- e.g. "I wanna see a report" the day after the nudge.
    """
    pending = get_pending_attendance_marking(whatsapp_id)
    if pending is None:
        return None

    group_label = pending["group_label"]
    activity_date = pending["activity_date"]
    roster = get_group_members(group_label)

    parsed = _parse_marking(message_text, len(roster))
    if parsed is None:
        return None

    kind, numbers = parsed
    if kind == "none":
        delete_pending_attendance_marking(whatsapp_id)
        _record(whatsapp_id, group_label, activity_date, len(roster), 0)
        asked = _send_feedback_to_attendees(roster, activity_date)
        return f"Thanks -- noted that everyone attended.{_feedback_note(asked)}"

    if kind == "bad_numbers":
        lines = [f"{i + 1}. {m['name']}" for i, m in enumerate(roster)]
        return (
            f"Those numbers don't match the list -- please use numbers from 1 to {len(roster)} "
            "(e.g. 2,5), or 'none'.\n\n" + "\n".join(lines)
        )

    # Deduplicated ("2,2" is one absence, not two -- matters now that the
    # absent count is recorded for reports), in the order the roster lists them.
    absent_members = [roster[n - 1] for n in numbers]
    delete_pending_attendance_marking(whatsapp_id)
    _record(whatsapp_id, group_label, activity_date, len(roster), len(absent_members))

    # The absence is always recorded -- attendance/membership status
    # never depends on follow-up consent. Only the "why did you miss
    # it?" message does: a member who texted STOP was promised no more
    # check-ins, and the leader is told so rather than it looking like
    # they were reached.
    reached, unreachable, opted_out = [], [], []
    for member in absent_members:
        absence_id = create_absence(member["reg_number"], ACTIVITY_TYPE_BIBLE_STUDY, activity_date, _now())
        if not member["whatsapp_id"]:
            unreachable.append(member["name"])
        elif not member["followup_consent"]:
            opted_out.append(member["name"])
        else:
            _start_reason_capture(member["whatsapp_id"], absence_id, member["name"])
            reached.append(member["name"])

    absent_reg_numbers = {m["reg_number"] for m in absent_members}
    attendees = [m for m in roster if m["reg_number"] not in absent_reg_numbers]
    asked = _send_feedback_to_attendees(attendees, activity_date)

    all_names = ", ".join(m["name"] for m in absent_members)
    reply = f"Got it -- marked {all_names} as absent."
    if reached:
        reply += f" Reached out directly to: {', '.join(reached)}."
    if unreachable:
        reply += f" No WhatsApp number on file for: {', '.join(unreachable)} -- couldn't reach them."
    if opted_out:
        reply += (
            f" Didn't message {', '.join(opted_out)} -- they've opted out of check-ins, "
            "so you may want to reach out personally."
        )
    reply += _feedback_note(asked)
    return reply


def _record(leader_whatsapp_id, group_label, activity_date, roster_size, absent_count):
    """Every marking is recorded -- including "none" -- so reports can tell full attendance from no marking (Stage 13)."""
    leader = get_member_by_whatsapp_id(leader_whatsapp_id)
    record_marking(group_label, activity_date, roster_size, absent_count,
                   leader["reg_number"] if leader else None, _now())


def _feedback_note(asked):
    """Tells the leader how many attendees were actually asked for feedback -- honestly, 0 included."""
    if asked == 0:
        return ""
    return f" Also sent a quick feedback question to the {asked} who attended."


def _send_feedback_to_attendees(attendees, activity_date):
    """
    Sends directly (the attendees are DIFFERENT people from the leader
    replying in this webhook turn), and only records a feedback request
    once the send actually succeeded -- a failed send must never count
    as a request that went unanswered, or it would drag the response
    rate down for the wrong reason. Returns how many were asked.
    """
    asked = 0
    for member in attendees:
        if not member["whatsapp_id"] or not member["followup_consent"]:
            continue
        message = feedback.build_feedback_prompt("Bible Study", member["name"])
        response = send_whatsapp_message(member["whatsapp_id"], message)
        if response.status_code == 200:
            feedback.begin_feedback(
                member["whatsapp_id"], member["reg_number"],
                ACTIVITY_TYPE_BIBLE_STUDY, activity_date, "bible_study",
            )
            asked += 1
    return asked


def _start_reason_capture(whatsapp_id, absence_id, name):
    """
    Sends directly rather than returning text, because the recipient
    here (the absent member) is a DIFFERENT person from whoever is
    replying in the current webhook turn (the leader who just marked
    them absent).
    """
    message = reason_capture.build_reason_prompt("Bible Study", name)
    response = send_whatsapp_message(whatsapp_id, message)
    if response.status_code == 200:
        reason_capture.begin_reason_capture(whatsapp_id, absence_id)
