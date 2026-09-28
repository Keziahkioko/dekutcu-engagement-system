"""
app/services/escalation.py

Stage 11: Escalation Manager. Two severity tiers for anything that
isn't an explicit "let me talk to a human" request:
  - "distress" -- asks for consent first ("would it be okay if I let
    a leader know?"). Only escalates on yes.
  - "acute_risk" -- escalates immediately, regardless of consent, but
    always TELLS the member this is happening rather than doing it
    silently.

This mirrors how real crisis-response systems handle the same
tension: respect the choice not to involve anyone for ordinary
distress, but override it specifically when there's a credible safety
risk, because protecting someone outweighs their momentary preference
for privacy at that point. The severity boundary is drawn by an LLM
classifier, not a clinician -- when genuinely ambiguous, this
deliberately leans toward "acute_risk", since an unnecessary check-in
costs far less than missing someone who actually needed help.

request_human is NOT part of this severity logic -- explicitly asking
to talk to a person already IS the consent, so it always escalates
directly (escalate_now), no question asked first.
"""

import json
from datetime import datetime, timezone

from app.models.member import get_leader_of_group, get_all_leaders, get_member_by_whatsapp_id
from app.models.escalation import create_escalation
from app.models.pending_escalation_consent import (
    get_pending_escalation_consent,
    start_pending_escalation_consent,
    delete_pending_escalation_consent,
)
from app.services.whatsapp_client import send_whatsapp_message
from app.services.llm_client import create_chat_completion

_SEVERITY_SYSTEM_PROMPT = (
    "You assess the severity of a message showing a member is struggling. "
    'Respond with ONLY a JSON object: {"severity": "<one of: none, distress, acute_risk>"}. '
    "acute_risk means the message suggests a credible, serious safety concern -- "
    "self-harm, suicidal thoughts, being in immediate danger, or similar. distress "
    "means real emotional struggle or difficulty, but with no such safety indicator. "
    "none means the message doesn't actually show distress at all. If genuinely "
    "unsure between distress and acute_risk, choose acute_risk -- an unnecessary "
    "check-in costs far less than missing someone who needed real help."
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def assess_severity(text):
    try:
        response = create_chat_completion(
            messages=[
                {"role": "system", "content": _SEVERITY_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content)
        severity = parsed.get("severity", "none")
        return severity if severity in ("none", "distress", "acute_risk") else "none"
    except Exception as e:
        try:
            print(f"Severity assessment failed, defaulting to 'distress' (safer than 'none'): {e}")
        except UnicodeEncodeError:
            print("Severity assessment failed, defaulting to 'distress' (safer than 'none') (error message omitted -- contained non-ASCII characters)")
        return "distress"


def find_target_leaders(member):
    """
    The member's own Bible Study group leader if they're placed and
    matched to one, otherwise every exec leader as a fallback (for
    members not yet in a group). Returns a list of (whatsapp_id, name,
    reg_number) tuples -- usually one, but every exec leader if there's
    no specific match. Public -- also used by the STOP opt-out notice
    (intent_router.opt_out_of_followup), so "which leader hears about
    this member" is decided in exactly one place.
    """
    if member.get("group_label"):
        leader = get_leader_of_group(member["group_label"])
        if leader and leader["whatsapp_id"]:
            return [(leader["whatsapp_id"], leader["name"], leader["reg_number"])]

    return [
        (l["whatsapp_id"], l["name"], l["reg_number"])
        for l in get_all_leaders() if l["whatsapp_id"]
    ]


def _notify_leaders(member, trigger_type, context_text, urgency_label):
    targets = find_target_leaders(member)
    for whatsapp_id, name, leader_reg_number in targets:
        notification = f"{member['name']} {urgency_label}"
        if context_text:
            notification += f" -- they said: \"{context_text}\""
        notification += "\n\nPlease reach out to them."
        send_whatsapp_message(whatsapp_id, notification)
        create_escalation(member["reg_number"], trigger_type, context_text, leader_reg_number, _now())
    return targets


def escalate_now(member, trigger_type, context_text):
    """For request_human, and for a distress escalation the member has already consented to."""
    targets = _notify_leaders(member, trigger_type, context_text, "may need some support")
    if targets:
        leader_name = targets[0][1] if len(targets) == 1 else "a leader"
        return f"I've let {leader_name} know -- they'll reach out to you soon."
    return "I wasn't able to find a leader to notify right now, but please don't hesitate to reach out to someone directly."


def escalate_acute(member, trigger_type, context_text):
    """For acute_risk -- escalates regardless of consent, always transparently."""
    targets = _notify_leaders(member, trigger_type, context_text, "may need urgent support")
    if targets:
        leader_name = targets[0][1] if len(targets) == 1 else "a leader"
        return (
            "What you're describing sounds really serious. Because of that, I've "
            f"already let {leader_name} know so they can reach out and support you "
            "as soon as possible."
        )
    return (
        "What you're describing sounds really serious, and I want to make sure you "
        "get real support -- please reach out to a leader directly as soon as you can."
    )


def start_consent_flow(whatsapp_id, trigger_type, context_text):
    """
    Persists pending state only -- does NOT send anything. Returns the
    prompt text for the caller to deliver however's appropriate for
    their situation (this is always the same person currently
    messaging in every caller so far, so it's returned as reply text,
    not sent directly).
    """
    start_pending_escalation_consent(whatsapp_id, trigger_type, context_text, _now())
    return (
        "Would it be okay if I let one of your leaders know, so they can check in "
        "with you?\n\nReply YES or NO."
    )


def handle_consent_reply(whatsapp_id, message_text):
    pending = get_pending_escalation_consent(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    answer = message_text.strip().lower()
    if answer not in ("yes", "no"):
        return "Please reply YES or NO."

    delete_pending_escalation_consent(whatsapp_id)

    if answer == "no":
        return "No problem -- please know you can always reach out anytime if that changes."

    member = get_member_by_whatsapp_id(whatsapp_id)
    return escalate_now(member, pending["trigger_type"], pending["context_text"])
