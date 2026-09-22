"""
app/services/intent_router.py

Stage 4: The intent router.

This only runs for messages from ALREADY-REGISTERED members who are
NOT mid-registration -- app/routes/webhook.py handles that ordering
(unregistered -> registration flow; pending -> continue registration;
only otherwise does a message reach this module).

LLM-based intent classification (Groq) drives dispatch for everything
except the hardcoded STOP/RESUME keywords, which webhook.py checks
globally, before even the registered/pending checks -- an opt-out (or
opt back in) should never depend on registration status or on an AI
classifier having a good day.

Design note on scope: several intents below are stubbed -- they
correctly recognize what the member wants, but the real handler
(e.g. actually showing upcoming events) belongs to a later stage
that hasn't been built yet. Three intents (update_details,
unsubscribe_followup, withdraw_data_consent) are genuinely handled
here since they only need the Members table, which already exists.
"""

import os
import json
from groq import Groq

from app.models.member import get_member_by_whatsapp_id
from app.models.pending_action import set_pending_action, get_pending_action, clear_pending_action
from app.database import get_connection

GROQ_MODEL = "openai/gpt-oss-20b"

STOP_KEYWORDS = {"stop", "unsubscribe"}
RESUME_KEYWORDS = {"resume"}

# Each intent's definition, shown to the LLM so it knows what each
# label means. Keep these short and behavior-focused.
INTENT_DEFINITIONS = {
    "greeting_smalltalk": "Casual greeting, small talk, thanks, or chit-chat with no specific request.",
    "general_question": "A question about the organization, its beliefs, or its activities.",
    "event_rsvp": "Asking about upcoming events, or responding to/RSVPing for one.",
    "checkin_response": "Explaining or giving a reason for missing a session or event.",
    "feedback_response": "Giving feedback, a rating, or comments about a past event.",
    "purchase_study_guide": "Wanting to buy or pay for a Bible Study guide.",
    "update_details": "Wanting to change their own registered details (e.g. area, year of study, name).",
    "unsubscribe_followup": "Wanting to stop receiving follow-up/accountability check-ins specifically.",
    "resume_followup": "Wanting to start receiving follow-up/accountability check-ins again, having previously stopped them.",
    "withdraw_data_consent": "Wanting to withdraw consent entirely and stop being tracked/registered.",
    "request_human": "Explicitly asking to speak with a real person or a leader.",
    "needs_support": "Message shows real distress or a serious personal struggle, even without explicitly asking for a human.",
    "leadership_query": "A leader asking for organizational data or a report (e.g. attendance numbers).",
    "send_announcement": "A leader wanting to broadcast a message to all members.",
    "unclear": "Doesn't confidently match any of the above.",
}

VALID_INTENTS = set(INTENT_DEFINITIONS.keys())

LEADER_ONLY_INTENTS = {"leadership_query", "send_announcement"}


def _build_system_prompt():
    lines = [
        "You classify an incoming WhatsApp message into exactly one intent label.",
        "Respond with ONLY a JSON object of the form {\"intent\": \"<label>\"}.",
        "Choose the single best-fitting label from this list:",
        "",
    ]
    for label, definition in INTENT_DEFINITIONS.items():
        lines.append(f"- {label}: {definition}")
    lines.append("")
    lines.append("If nothing fits confidently, use \"unclear\".")
    return "\n".join(lines)


_SYSTEM_PROMPT = _build_system_prompt()


def is_stop_message(text):
    """Hardcoded, non-LLM check for opting OUT of follow-up check-ins."""
    return text.strip().lower() in STOP_KEYWORDS


def is_resume_message(text):
    """Hardcoded, non-LLM check for opting back IN to follow-up check-ins."""
    return text.strip().lower() in RESUME_KEYWORDS


def classify_intent(message_text):
    """
    Calls Groq to classify the message into one of the intents in
    INTENT_DEFINITIONS. Falls back to "unclear" on any API error or
    unparseable/invalid response.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return "unclear"

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": message_text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        intent = parsed.get("intent", "").strip()

        if intent in VALID_INTENTS:
            return intent
        return "unclear"

    except Exception as e:
        print(f"Intent classification failed, falling back to 'unclear': {e}")
        return "unclear"


def set_followup_consent(whatsapp_id, value):
    """
    Sets ONLY followup_consent for a registered member -- shared by
    the global STOP/RESUME handlers and the unsubscribe_followup
    intent. Deliberately does NOT touch data_consent -- that's a
    separate, more serious action (withdraw_data_consent).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET followup_consent = %s WHERE whatsapp_id = %s",
        (value, whatsapp_id)
    )
    conn.commit()
    cursor.close()
    conn.close()


CONFIRMATION_QUESTIONS = {
    "unsubscribe_followup": (
        "Just to confirm: you'll stop receiving follow-up check-ins if you "
        "miss Bible study, fellowships, or events. You'll still be a fully "
        "registered member -- this only affects check-ins, nothing else.\n\n"
        "Reply YES to confirm, or NO to cancel."
    ),
    "resume_followup": (
        "Just to confirm: you'll start receiving follow-up check-ins again "
        "if you miss something.\n\n"
        "Reply YES to confirm, or NO to cancel."
    ),
    "withdraw_data_consent": (
        "This is different from just pausing check-ins. Withdrawing consent "
        "fully removes you from tracking -- your Bible Study group "
        "placement, membership records, and anything tied to welfare "
        "eligibility. If you only want to stop check-ins, reply NO here "
        "and text STOP instead.\n\n"
        "Reply YES to confirm you want to withdraw completely, or NO to cancel."
    ),
}


def _handle_unsubscribe_followup(member, text):
    set_pending_action(member["whatsapp_id"], "unsubscribe_followup")
    return CONFIRMATION_QUESTIONS["unsubscribe_followup"]


def _handle_resume_followup(member, text):
    set_pending_action(member["whatsapp_id"], "resume_followup")
    return CONFIRMATION_QUESTIONS["resume_followup"]


def _handle_withdraw_data_consent(member, text):
    set_pending_action(member["whatsapp_id"], "withdraw_data_consent")
    return CONFIRMATION_QUESTIONS["withdraw_data_consent"]


def withdraw_all_consent(whatsapp_id):
    """
    Sets BOTH consent flags to False -- the full, deliberate
    withdrawal, distinct from STOP/unsubscribe_followup which only
    ever touches followup_consent.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET data_consent = FALSE, followup_consent = FALSE WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()


def handle_pending_action_response(whatsapp_id, text):
    """
    Called by webhook.py when a member has a pending action awaiting
    YES/NO confirmation. Executes or cancels it accordingly. Returns
    the reply text.
    """
    pending = get_pending_action(whatsapp_id)
    if pending is None:
        return None  # shouldn't happen -- webhook.py only calls this when one exists

    action = pending["action"]
    answer = text.strip().lower()

    if answer not in ("yes", "no"):
        # Re-ask rather than silently falling through to the intent
        # router -- a half-confirmed serious action shouldn't be lost
        # to an ambiguous reply.
        return f"Please reply YES or NO.\n\n{CONFIRMATION_QUESTIONS[action]}"

    clear_pending_action(whatsapp_id)

    if answer == "no":
        return "No problem, nothing has changed."

    if action == "unsubscribe_followup":
        set_followup_consent(whatsapp_id, False)
        return (
            "You won't receive follow-up check-ins anymore. You're still "
            "a fully registered member -- reply 'resume' anytime if you "
            "change your mind."
        )
    elif action == "resume_followup":
        set_followup_consent(whatsapp_id, True)
        return "Great, follow-up check-ins are back on."
    elif action == "withdraw_data_consent":
        withdraw_all_consent(whatsapp_id)
        return (
            "Your consent has been withdrawn and you're no longer an "
            "active tracked member. If you'd like to fully rejoin later, "
            "just message me again to re-register."
        )

    return "Something went wrong processing that -- please try again."


def handle_message(whatsapp_id, message_text):
    """
    Main entry point for a message from a confirmed, fully-registered
    member (not mid-registration). Returns the reply text to send.

    Note: STOP/RESUME are checked globally in webhook.py, before even
    the registered/pending-registration checks -- by the time a
    message reaches this function, it's already known not to be one.
    """
    member = get_member_by_whatsapp_id(whatsapp_id)
    intent = classify_intent(message_text)

    if intent in LEADER_ONLY_INTENTS and not member["is_leader"]:
        # Don't confirm the feature exists to a non-leader -- just
        # fall back to the generic "can't help with that" response.
        intent = "unclear"

    handler = _STUB_HANDLERS.get(intent, _handle_unclear)
    return handler(member, message_text)


# ---------------------------------------------------------------------
# Handlers. update_details, unsubscribe_followup, and
# withdraw_data_consent will be replaced with real multi-step logic
# in the next build step -- stubbed here for now so the full router
# can be tested end-to-end first.
# ---------------------------------------------------------------------

def _handle_unclear(member, text):
    return "Sorry, I didn't quite catch that. Could you rephrase, or let me know what you're looking for?"


def _handle_stub(feature_name):
    def handler(member, text):
        return f"({feature_name} is coming in a later stage -- thanks for your patience!)"
    return handler


_STUB_HANDLERS = {
    "greeting_smalltalk": lambda member, text: f"Hey {member['name'].split()[0]}! How can I help?",
    "general_question": _handle_stub("Answering general questions"),
    "event_rsvp": _handle_stub("Event info and RSVPs"),
    "checkin_response": _handle_stub("Check-in handling"),
    "feedback_response": _handle_stub("Feedback collection"),
    "purchase_study_guide": _handle_stub("Study guide payments"),
    "update_details": _handle_stub("Updating your details"),
    "unsubscribe_followup": _handle_unsubscribe_followup,
    "resume_followup": _handle_resume_followup,
    "withdraw_data_consent": _handle_withdraw_data_consent,
    "request_human": _handle_stub("Connecting you to a leader"),
    "needs_support": _handle_stub("Connecting you to a leader"),
    "leadership_query": _handle_stub("Leadership reports"),
    "send_announcement": _handle_stub("Sending announcements"),
    "unclear": _handle_unclear,
}
