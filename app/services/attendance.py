"""
app/services/attendance.py

Stage 7: the Bible Study side of reason capture -- the scheduled
Tuesday-evening leader nudge, the leader's absence-marking reply, and
the reason-capture conversation with each absent member (LLM
classification into the six categories from the proposal, plus a
distress safety-net).

Deliberately Bible-Study-only for now -- the open-fellowship
check-in/regular-inference side is separate, not-yet-built machinery
(see PROJECT_LOG.md's Objective 3 entry), even though it will
eventually write to the same `absences` table.

The distress flag is deliberately just a flag, not an escalation --
Stage 11 (Escalation Manager) doesn't exist yet. This makes sure a
serious reply gets a real, human-pointing response instead of the
same generic "thanks for sharing" everything else gets, without
pretending to be a full escalation system it isn't yet.
"""

import json
from datetime import datetime, timezone, date

from app.models.member import get_data_consenting_members, get_group_members
from app.models.pending_attendance_marking import (
    get_pending_attendance_marking,
    start_pending_attendance_marking,
    delete_pending_attendance_marking,
)
from app.models.pending_reason_capture import (
    get_pending_reason_capture,
    start_pending_reason_capture,
    delete_pending_reason_capture,
)
from app.models.absence import create_absence, record_reason
from app.services.whatsapp_client import send_whatsapp_message
from app.services.llm_client import create_chat_completion

ACTIVITY_TYPE_BIBLE_STUDY = "bible_study"

_REASON_CATEGORIES = [
    "scheduling_conflict", "health", "personal_difficulty",
    "logistical_barrier", "disengagement", "unclassified",
]

_CLASSIFIER_SYSTEM_PROMPT = (
    "You classify why a Bible Study member missed a session, based on their own "
    "words. Respond with ONLY a JSON object: "
    '{"category": "<one of: scheduling_conflict, health, personal_difficulty, '
    'logistical_barrier, disengagement, unclassified>", "shows_distress": <true/false>}. '
    "shows_distress should be true if the reply suggests something serious -- real "
    "emotional struggle, a crisis, anything beyond a routine, low-stakes reason -- "
    "not just because the reason itself is unfortunate (e.g. a scheduling conflict "
    "or minor illness is NOT distress; something like feeling hopeless, overwhelmed, "
    "or unsafe IS)."
)


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
    message = (
        f"Hey {name.split()[0]}, we noticed you weren't able to make it to Bible Study "
        "today. Would you mind sharing why? This helps us support you better.\n\n"
        "Reply 'skip' if you'd rather not say."
    )
    response = send_whatsapp_message(whatsapp_id, message)
    if response.status_code == 200:
        start_pending_reason_capture(whatsapp_id, absence_id, _now())


def handle_reason_capture_message(whatsapp_id, message_text):
    text = message_text.strip()
    pending = get_pending_reason_capture(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    delete_pending_reason_capture(whatsapp_id)

    if text.lower() == "skip":
        record_reason(pending["absence_id"], None, "unclassified", False)
        return "No problem -- thanks for letting us know either way."

    category, shows_distress = _classify_reason(text)
    record_reason(pending["absence_id"], text, category, shows_distress)

    if shows_distress:
        return (
            "Thank you for sharing that, and I'm really sorry you're going through this. "
            "Please don't hesitate to reach out to one of your leaders directly -- they "
            "genuinely want to support you."
        )
    return "Thanks for sharing -- we appreciate you letting us know."


def _classify_reason(text):
    try:
        response = create_chat_completion(
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content)
        category = parsed.get("category", "unclassified")
        if category not in _REASON_CATEGORIES:
            category = "unclassified"
        shows_distress = bool(parsed.get("shows_distress", False))
        return category, shows_distress
    except Exception as e:
        print(f"Reason classification failed, defaulting to unclassified: {e}")
        return "unclassified", False
