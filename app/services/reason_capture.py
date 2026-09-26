"""
app/services/reason_capture.py

Shared reason-capture + classification logic used by BOTH the Bible
Study slice (attendance.py) and the open-fellowship slice
(fellowship_checkin.py) of Stage 7 -- extracted here once a second
caller actually needed it, rather than built speculatively ahead of
need.

Deliberately split into "build the prompt text" / "persist the
pending state" / "handle the reply" as separate pieces rather than one
function that sends a message directly: Bible Study's leader-nudge
flow needs to message a DIFFERENT person than the one currently
replying (the absent member, not the leader), while the fellowship
check-in's bare-"no" case needs to reply to the SAME person who just
messaged -- returning text through the normal reply path rather than
sending a second, separate message. Same underlying mechanism, two
different callers wiring it up differently.
"""

import json
from datetime import datetime, timezone

from app.models.pending_reason_capture import (
    get_pending_reason_capture,
    start_pending_reason_capture,
    delete_pending_reason_capture,
)
from app.models.absence import record_reason, get_absence_by_id
from app.services.llm_client import create_chat_completion
from app.services import bandit

_REASON_CATEGORIES = [
    "scheduling_conflict", "health", "personal_difficulty",
    "logistical_barrier", "disengagement", "unclassified",
]

_CLASSIFIER_SYSTEM_PROMPT = (
    "You classify why a member missed an activity, based on their own words. "
    "Respond with ONLY a JSON object: "
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


def build_reason_prompt(activity_description, name=None):
    greeting = f"Hey {name.split()[0]}, we" if name else "We"
    return (
        f"{greeting} noticed you weren't able to make it to {activity_description}. "
        "Would you mind sharing why? This helps us support you better.\n\n"
        "Reply 'skip' if you'd rather not say."
    )


def begin_reason_capture(whatsapp_id, absence_id):
    """Persists the pending state only -- does NOT send anything."""
    start_pending_reason_capture(whatsapp_id, absence_id, _now())


def classify_reason(text):
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


def _select_strategy_message(absence_id, reason_category):
    """
    Stage 8 hook: once a reason is on record, the bandit picks a
    follow-up strategy and this returns the message for it. Only
    called for the NON-distress path -- a distress reply already gets
    its own, more important response pointing to a human leader, and
    shouldn't also be treated as ordinary bandit-training fodder mixed
    in with routine re-engagement optimization.
    """
    absence = get_absence_by_id(absence_id)
    return bandit.select_arm_for_absence(
        absence_id, absence["reg_number"], absence["activity_type"],
        absence["activity_date"], reason_category,
    )


def classify_and_record(absence_id, text):
    """
    Classifies raw text and records it against the given absence,
    returning the reply to send -- the routine ack (plus a bandit
    -selected follow-up strategy), or the distress safety-net reply.
    Shared by the interactive (pending_reason_capture) reply path and
    the fellowship slice's "already explained themselves unprompted,
    in the same message" path.
    """
    category, shows_distress = classify_reason(text)
    record_reason(absence_id, text, category, shows_distress)

    if shows_distress:
        return (
            "Thank you for sharing that, and I'm really sorry you're going through this. "
            "Please don't hesitate to reach out to one of your leaders directly -- they "
            "genuinely want to support you."
        )

    strategy_message = _select_strategy_message(absence_id, category)
    return f"Thanks for sharing -- we appreciate you letting us know.\n\n{strategy_message}"


def handle_reason_capture_message(whatsapp_id, message_text):
    text = message_text.strip()
    pending = get_pending_reason_capture(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    delete_pending_reason_capture(whatsapp_id)

    if text.lower() == "skip":
        record_reason(pending["absence_id"], None, "unclassified", False)
        strategy_message = _select_strategy_message(pending["absence_id"], "unclassified")
        return f"No problem -- thanks for letting us know either way.\n\n{strategy_message}"

    return classify_and_record(pending["absence_id"], text)
