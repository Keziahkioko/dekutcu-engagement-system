"""
app/services/attendance.py

Stage 7: the Bible Study side of reason capture -- the scheduled
Tuesday-evening leader nudge, and the leader's absence-marking reply.
Reason capture itself (classification, the distress safety-net) is
shared machinery -- see reason_capture.py -- since the open-fellowship
slice (fellowship_checkin.py) needs the exact same thing.

Deliberately Bible-Study-only for now -- see PROJECT_LOG.md's
Objective 3 entry for how this and the open-fellowship slice share the
same `absences` table without sharing everything else.
"""

from datetime import datetime, timezone, date

from app.models.member import get_data_consenting_members, get_group_members
from app.models.pending_attendance_marking import (
    get_pending_attendance_marking,
    start_pending_attendance_marking,
    delete_pending_attendance_marking,
)
from app.models.absence import create_absence
from app.services.whatsapp_client import send_whatsapp_message
from app.services import reason_capture

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


def handle_attendance_marking_message(whatsapp_id, message_text):
    text = message_text.strip()
    pending = get_pending_attendance_marking(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    group_label = pending["group_label"]
    activity_date = pending["activity_date"]
    roster = get_group_members(group_label)

    if text.lower() == "none":
        delete_pending_attendance_marking(whatsapp_id)
        return "Thanks -- noted that everyone attended."

    numbers = [n.strip() for n in text.split(",")]
    if not numbers or not all(n.isdigit() and 1 <= int(n) <= len(roster) for n in numbers):
        lines = [f"{i + 1}. {m['name']}" for i, m in enumerate(roster)]
        return (
            "Please reply with numbers separated by commas (e.g. 2,5), or 'none'.\n\n"
            + "\n".join(lines)
        )

    absent_members = [roster[int(n) - 1] for n in numbers]
    delete_pending_attendance_marking(whatsapp_id)

    reached, unreachable = [], []
    for member in absent_members:
        absence_id = create_absence(member["reg_number"], ACTIVITY_TYPE_BIBLE_STUDY, activity_date, _now())
        if member["whatsapp_id"]:
            _start_reason_capture(member["whatsapp_id"], absence_id, member["name"])
            reached.append(member["name"])
        else:
            unreachable.append(member["name"])

    all_names = ", ".join(m["name"] for m in absent_members)
    reply = f"Got it -- marked {all_names} as absent."
    if reached:
        reply += f" Reached out directly to: {', '.join(reached)}."
    if unreachable:
        reply += f" No WhatsApp number on file for: {', '.join(unreachable)} -- couldn't reach them."
    return reply


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
