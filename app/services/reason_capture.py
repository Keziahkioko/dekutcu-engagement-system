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

Severity assessment (Stage 11) is deliberately a SEPARATE call to
escalation.py rather than folded into this module's own classifier
output, even though that costs an extra LLM round trip -- severity is
safety-relevant and needs to use the exact same logic everywhere it's
checked (here, and the general needs_support intent in
intent_router.py), not two similar-but-not-identical criteria that
could disagree with each other.
"""

import json
from datetime import datetime, timezone

from app.models.pending_reason_capture import (
    get_pending_reason_capture,
    start_pending_reason_capture,
    delete_pending_reason_capture,
)
from app.models.absence import record_reason, get_absence_by_id
from app.models.member import get_member_by_whatsapp_id, get_member_by_reg_number
from app.services.llm_client import create_chat_completion
from app.services import bandit
from app.services import escalation
from app.services import message_generator

_REASON_CATEGORIES = [
    "scheduling_conflict", "health", "personal_difficulty",
    "logistical_barrier", "disengagement", "unclassified",
]

_CLASSIFIER_SYSTEM_PROMPT = (
    "You classify why a member missed an activity, based on their own words. "
    "Respond with ONLY a JSON object: "
    '{"category": "<one of: scheduling_conflict, health, personal_difficulty, '
    'logistical_barrier, disengagement, unclassified>"}.'
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def build_reason_prompt(activity_description, name=None):
    """
    For Bible Study, where a LEADER marked the member absent -- the only
    case where the bot genuinely "noticed". The other two situations get
    their own wording below: found in the first live test, a member who
    had just replied "no" was told "We noticed you weren't able to make
    it", which read like surveillance.
    """
    greeting = f"Hey {name.split()[0]}, we" if name else "We"
    return (
        f"{greeting} noticed you weren't able to make it to {activity_description}. "
        "Would you mind sharing why? This helps us support you better.\n\n"
        "Reply 'skip' if you'd rather not say."
    )


def build_live_reason_prompt(name=None):
    """The member has JUST told us they weren't there (a bare "no" to a check-in)."""
    thanks = f"Thanks for letting us know, {name.split()[0]}." if name else "Thanks for letting us know."
    return (
        f"{thanks} Would you mind sharing what kept you away? It helps us support you better.\n\n"
        "Reply 'skip' if you'd rather not say."
    )


def build_silence_reason_prompt(activity_description, name=None):
    """
    A regular didn't reply to the check-in (the noon sweep). Absence is only
    INFERRED from silence -- they may have been there and just not replied --
    so this never states as fact that they weren't.
    """
    greeting = f"Hey {name.split()[0]}, we" if name else "We"
    return (
        f"{greeting} didn't hear back after yesterday's {activity_description} check-in, "
        "and we missed you! If you weren't able to make it, would you mind sharing what "
        "kept you away?\n\nReply 'skip' if you'd rather not say."
    )


def begin_reason_capture(whatsapp_id, absence_id):
    """Persists the pending state only -- does NOT send anything."""
    start_pending_reason_capture(whatsapp_id, absence_id, _now())


def classify_reason(text):
    """Classifies into one of the six absence-reason categories only -- severity is assess_severity's job, see module docstring."""
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
        return category if category in _REASON_CATEGORIES else "unclassified"
    except Exception as e:
        try:
            print(f"Reason classification failed, defaulting to unclassified: {e}")
        except UnicodeEncodeError:
            print("Reason classification failed, defaulting to unclassified (error message omitted -- contained non-ASCII characters)")
        return "unclassified"


def _select_strategy_message(absence_id, reason_category):
    """
    Stage 8/9 hook: once a reason is on record, the bandit picks a
    follow-up strategy (pure math, no API call -- see bandit.py) and
    message_generator turns that into the actual live-generated text,
    using the member's own stated reason (absence["reason_raw"], which
    is None for a 'skip' reply or a silent-lapse absence -- handled by
    message_generator itself). Only called for the "none" severity
    path -- distress/acute_risk replies get their own, more important
    escalation response instead, and shouldn't also be treated as
    ordinary bandit-training fodder mixed in with routine
    re-engagement optimization.
    """
    absence = get_absence_by_id(absence_id)
    arm = bandit.select_arm_for_absence(
        absence_id, absence["reg_number"], absence["activity_type"],
        absence["activity_date"], reason_category,
    )
    member = get_member_by_reg_number(absence["reg_number"])
    member_name = member["name"] if member else None
    return message_generator.generate_arm_message(
        arm, absence["activity_type"], absence["reason_raw"], member_name,
    )


def classify_and_record(whatsapp_id, absence_id, text):
    """
    Classifies raw text and records it against the given absence,
    returning the reply to send. Shared by the interactive
    (pending_reason_capture) reply path and the fellowship slice's
    "already explained themselves unprompted, in the same message"
    path -- both always reply to the SAME person currently messaging,
    so escalation's consent-ask/acute-risk text is returned directly
    rather than sent as a separate message.
    """
    category = classify_reason(text)
    severity = escalation.assess_severity(text)
    record_reason(absence_id, text, category, severity)

    if severity == "acute_risk":
        member = get_member_by_whatsapp_id(whatsapp_id)
        return escalation.escalate_acute(member, "reason_capture", text)

    if severity == "distress":
        return escalation.start_consent_flow(whatsapp_id, "reason_capture", text)

    strategy_message = _select_strategy_message(absence_id, category)
    return f"Thanks for sharing -- we appreciate you letting us know.\n\n{strategy_message}"


def handle_reason_capture_message(whatsapp_id, message_text):
    text = message_text.strip()
    pending = get_pending_reason_capture(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    delete_pending_reason_capture(whatsapp_id)

    if text.lower() == "skip":
        record_reason(pending["absence_id"], None, "unclassified", "none")
        strategy_message = _select_strategy_message(pending["absence_id"], "unclassified")
        return f"No problem -- thanks for letting us know either way.\n\n{strategy_message}"

    return classify_and_record(whatsapp_id, pending["absence_id"], text)
