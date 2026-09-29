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

Stage 12 adds a softer third kind: a leader OFFER (start_leader_offer)
after a RAG answer to a pastoral question, or when DeKUTCU's materials
don't cover something. Same consent-first rule -- a leader only on YES.
Neither kind of question is insistent any more (the distress question
used to re-ask "Please reply YES or NO" until answered -- removed after
the first live test, see handle_consent_reply): any reply that isn't a
yes or a no drops the question and the message is handled normally.
The two kinds now differ only in wording and in which triggers
(OFFER_TRIGGERS) count as a casual offer.
"""

import re
import json
from datetime import datetime, timezone, timedelta

from app.models.member import (
    get_leader_of_group, get_all_leaders, get_member_by_whatsapp_id, get_member_by_reg_number,
)
from app.models.escalation import (
    create_escalation, create_case, claim_case, get_case, get_recent_case,
    notified_leaders, open_cases_for_leader,
)
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

    NEVER includes the member themselves -- found in the first live
    test: a leader asking for help was notified about themselves and
    told "I've let [themselves] know". A group leader in their own group
    falls back to the exec leaders; if nobody else is left, the list is
    empty and callers say they couldn't find anyone.
    """
    own = member.get("reg_number")
    if member.get("group_label"):
        leader = get_leader_of_group(member["group_label"])
        if leader and leader["whatsapp_id"] and leader["reg_number"] != own:
            return [(leader["whatsapp_id"], leader["name"], leader["reg_number"])]

    return [
        (l["whatsapp_id"], l["name"], l["reg_number"])
        for l in get_all_leaders() if l["whatsapp_id"] and l["reg_number"] != own
    ]


# A leader already notified about this member within this window is not
# notified again for a repeat request or distress, and the member isn't
# asked the consent question again -- they're told it's in hand. Found in
# the live test: "Can I get their number?" one minute after asking for a
# leader re-triggered the consent question. Acute risk ALWAYS escalates
# again regardless.
RECENT_ESCALATION_WINDOW = timedelta(hours=6)


def _recent_case(member):
    since = (datetime.now(timezone.utc) - RECENT_ESCALATION_WINDOW).isoformat()
    return get_recent_case(member["reg_number"], since)


def _already_in_hand(case):
    """What to tell a member who already has a recent case open."""
    if case["claimed_by_reg_number"]:
        leader = get_member_by_reg_number(case["claimed_by_reg_number"])
        who = leader["name"] if leader else "A leader"
        return f"{who} already knows and will be reaching out to you soon."
    return "I've already let your leaders know -- one of them will be in touch soon."


def _whatsapp_number(whatsapp_id):
    return f"+{whatsapp_id}" if whatsapp_id and not whatsapp_id.startswith("+") else whatsapp_id


def _notify_leaders(member, trigger_type, context_text, urgency_label):
    """
    Opens a case and notifies every target leader. Every notification
    includes the member's WhatsApp number (Keziah's decision: they agreed
    to be contacted, or it's acute risk where speed matters) so the
    leader can actually reach them. With ONE target the case is theirs
    automatically; with several, each is asked to reply CLAIM <case> and
    the first to do so takes it (see handle_claim).
    """
    targets = find_target_leaders(member)
    if not targets:
        return None, targets

    case_id = create_case(member["reg_number"], trigger_type, context_text, _now())
    if len(targets) == 1:
        claim_case(case_id, targets[0][2], _now())

    for whatsapp_id, name, leader_reg_number in targets:
        notification = f"{member['name']} {urgency_label}"
        if context_text:
            notification += f" -- they said: \"{context_text}\""
        notification += f"\n\nTheir WhatsApp: {_whatsapp_number(member['whatsapp_id'])}"
        if len(targets) == 1:
            notification += "\n\nPlease reach out to them."
        else:
            notification += (
                f"\n\n{len(targets)} leaders were told about this. If you'll reach out, "
                f"reply CLAIM {case_id} -- the first to reply takes it, and the others "
                "will be told so nobody doubles up."
            )
        send_whatsapp_message(whatsapp_id, notification)
        create_escalation(member["reg_number"], trigger_type, context_text, leader_reg_number, _now(), case_id)
    return case_id, targets


def _told_member(targets):
    if len(targets) == 1:
        return f"I've let {targets[0][1]} know -- they'll reach out to you soon."
    return "I've let your leaders know -- I'll tell you who's reaching out as soon as one of them takes it."


def escalate_now(member, trigger_type, context_text, urgency_label="may need some support"):
    """For request_human, a distress escalation the member has consented to, or an accepted leader offer."""
    recent = _recent_case(member)
    if recent:
        return _already_in_hand(recent)
    _case_id, targets = _notify_leaders(member, trigger_type, context_text, urgency_label)
    if targets:
        return _told_member(targets)
    return "I wasn't able to find a leader to notify right now, but please don't hesitate to reach out to someone directly."


def escalate_acute(member, trigger_type, context_text):
    """For acute_risk -- escalates regardless of consent, always transparently, and ALWAYS again even if recent."""
    _case_id, targets = _notify_leaders(member, trigger_type, context_text, "may need urgent support")
    if targets:
        who = targets[0][1] if len(targets) == 1 else "your leaders"
        return (
            "What you're describing sounds really serious. Because of that, I've "
            f"already let {who} know so they can reach out and support you "
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
    not sent directly). If a leader was already notified about this
    member recently, no question is asked -- they're told it's in hand.
    """
    member = get_member_by_whatsapp_id(whatsapp_id)
    recent = _recent_case(member) if member else None
    if recent:
        return _already_in_hand(recent)
    start_pending_escalation_consent(whatsapp_id, trigger_type, context_text, _now())
    # Acknowledge first -- found in the first live test: "I was just feeling
    # down" got the consent question with nothing before it. One fixed,
    # model-free sentence, used wherever distress is detected.
    return (
        "I'm really sorry things have been hard. Thank you for being honest with me.\n\n"
        "Would it be okay if I let one of your leaders know, so they can check in "
        "with you?\n\nReply YES or NO."
    )


OFFER_TRIGGERS = {"pastoral_question", "rag_not_covered", "rag_secondary_issue"}

OFFER_TEXT = "Would you like one of your leaders to reach out to you personally? Reply YES or NO."


def start_leader_offer(whatsapp_id, trigger_type, context_text):
    """
    The soft offer -- see module docstring. Persists the pending
    question and returns the offer text for the caller to append to its
    reply. trigger_type must be one of OFFER_TRIGGERS, which is what
    makes handle_consent_reply treat it as an offer rather than the
    distress question (casual wording and replies).
    """
    assert trigger_type in OFFER_TRIGGERS
    member = get_member_by_whatsapp_id(whatsapp_id)
    recent = _recent_case(member) if member else None
    if recent:
        return _already_in_hand(recent)
    start_pending_escalation_consent(whatsapp_id, trigger_type, context_text, _now())
    return OFFER_TEXT


_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "yes please", "ok please", "okay please"}
_NO = {"no", "nope", "nah", "no thanks", "no thank you", "not now", "no please"}


def handle_consent_reply(whatsapp_id, message_text):
    """
    Returns the reply -- or None if the answer is neither a yes nor a no:
    the question is dropped and webhook.py handles the message normally.

    This used to be STRICT for the distress question ("Please reply YES
    or NO" until they did). Removed at Keziah's request after the first
    live test, where it left her stuck. Safe because normal routing runs
    the safety check again on whatever the member wrote instead: more
    about how they feel is re-assessed as distress (and asked again) or
    as acute risk (escalated at once) -- nothing safety-relevant is lost,
    people just can't get trapped. Everyday answers ("yeah", "sure",
    "nah", "not now") count, not only the literal YES/NO.
    """
    pending = get_pending_escalation_consent(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    is_offer = pending["trigger_type"] in OFFER_TRIGGERS
    answer = " ".join(message_text.strip().lower().rstrip(".!").split())
    if answer in _YES:
        answer = "yes"
    elif answer in _NO:
        answer = "no"
    else:
        delete_pending_escalation_consent(whatsapp_id)
        return None

    delete_pending_escalation_consent(whatsapp_id)

    if answer == "no":
        if is_offer:
            return "No problem!"
        return "No problem -- please know you can always reach out anytime if that changes."

    member = get_member_by_whatsapp_id(whatsapp_id)
    if is_offer:
        return escalate_now(member, pending["trigger_type"], pending["context_text"], "would like a leader to reach out to them")
    return escalate_now(member, pending["trigger_type"], pending["context_text"])


# ---------------------------------------------------------------------
# Claiming a case -- when several leaders were notified about one
# member, the first to reply "CLAIM <case>" takes it; the others are
# told who has it so nobody doubles up, and the member is told who's
# coming. Handled early in webhook.py (before any pending flow), since a
# leader may be mid-way through something else when they reply.
# ---------------------------------------------------------------------

_CLAIM_PATTERN = re.compile(r"^\s*claim\s*#?\s*(\d+)?\s*[.!]?\s*$", re.IGNORECASE)


def is_claim_message(message_text):
    return _CLAIM_PATTERN.match(message_text) is not None


def handle_claim(leader_whatsapp_id, message_text):
    """
    Returns the reply to the claiming leader, or None if this sender has
    nothing to claim (then the message is routed normally -- "claim" on
    its own from a non-leader shouldn't be swallowed).
    """
    leader = get_member_by_whatsapp_id(leader_whatsapp_id)
    if not leader:
        return None
    number = _CLAIM_PATTERN.match(message_text).group(1)

    if number is None:
        open_cases = open_cases_for_leader(leader["reg_number"])
        if not open_cases:
            return None
        if len(open_cases) > 1:
            lines = []
            for c in open_cases:
                who = get_member_by_reg_number(c["reg_number"])
                lines.append(f"CLAIM {c['id']} -- {who['name'] if who else c['reg_number']}")
            return "You have more than one open case -- reply with the one you're taking:\n\n" + "\n".join(lines)
        case_id = open_cases[0]["id"]
    else:
        case_id = int(number)

    case = get_case(case_id)
    if not case or leader["reg_number"] not in notified_leaders(case_id):
        return None

    member = get_member_by_reg_number(case["reg_number"])
    member_name = member["name"] if member else "the member"
    number_text = _whatsapp_number(member["whatsapp_id"]) if member and member["whatsapp_id"] else "no WhatsApp number on file"

    if not claim_case(case_id, leader["reg_number"], _now()):
        claimed_by = get_case(case_id)["claimed_by_reg_number"]
        if claimed_by == leader["reg_number"]:
            return f"This one's already yours -- {member_name}: {number_text}."
        taken_by = get_member_by_reg_number(claimed_by)
        who = taken_by["name"] if taken_by else "another leader"
        return f"Thanks -- {who} has already taken this one, so no need to reach out to {member_name}."

    for other_reg in notified_leaders(case_id):
        if other_reg == leader["reg_number"]:
            continue
        other = get_member_by_reg_number(other_reg)
        if other and other["whatsapp_id"]:
            send_whatsapp_message(
                other["whatsapp_id"],
                f"{leader['name']} is reaching out to {member_name} -- no need for you to as well. Thank you!",
            )
    if member and member["whatsapp_id"]:
        send_whatsapp_message(member["whatsapp_id"], f"{leader['name']} will be reaching out to you soon.")

    return f"Thanks -- it's yours. {member_name}: {number_text}. The other leaders and {member_name} have been told."
