"""
app/services/registration.py

Stage 3: The registration/consent conversation.

This module handles ONE incoming message at a time. It doesn't know
about WhatsApp itself -- it just takes (whatsapp_id, message_text) and
returns the reply text to send back. app/routes/webhook.py is
responsible for actually calling the WhatsApp API to send that reply.

Design limits (deliberate, not oversights -- see Stage 3 discussion):
  - Free-text fields (name) are accepted as-is and only checked at
    the final confirmation step, since there's no way yet to tell
    "Kevin" apart from "hello" as a piece of text alone.
  - Fixed-choice fields (gender, year, area) ARE validated against
    their known valid options, since that's possible without needing
    real intent detection.
  - "cancel" is a universal escape hatch at any step.
  - Recognizing an off-topic question mid-registration and answering
    it (rather than swallowing it as a form answer) is intent-router
    territory (Stage 4) and out of scope here.
"""

from datetime import datetime, timezone

from app.models.member import (
    get_member_by_whatsapp_id,
    get_member_by_reg_number,
    create_member,
    relink_whatsapp_id,
)
from app.models.pending_registration import (
    get_pending_registration,
    start_pending_registration,
    update_pending_registration,
    delete_pending_registration,
)

ORG_NAME = "DeKUT CU"  # adjust to the organization's actual name/branding

AREAS = [
    "Bomas", "Internal Hostels", "Nyeri View", "Catholic Hostels",
    "Nyaribo", "Embassy", "King'ong'o", "Nyeri Town", "Gate A", "Kahawa",
]

GENDER_MAP = {
    "m": "Male", "male": "Male",
    "f": "Female", "female": "Female",
    "o": "Other", "other": "Other",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _areas_list_text():
    lines = [f"{i + 1}. {area}" for i, area in enumerate(AREAS)]
    return "\n".join(lines)


def is_registered(whatsapp_id):
    """True if this whatsapp_id already belongs to a confirmed member."""
    return get_member_by_whatsapp_id(whatsapp_id) is not None


def start_registration(whatsapp_id):
    """
    Called the very first time an unrecognized whatsapp_id messages
    the bot. Creates the pending registration row and returns the
    greeting + first question. This first incoming message is NOT
    treated as an answer to anything -- it's just what triggered the
    bot to introduce itself.
    """
    start_pending_registration(whatsapp_id, _now())
    return (
        f"Hi! I'm the {ORG_NAME} Engagement Bot \U0001F44B I help connect you "
        "with your Bible study group, events, and updates. To get started, "
        "I'll need a few quick details.\n\n"
        "(You can reply 'cancel' at any point to stop.)\n\n"
        "What's your school registration number?"
    )


def handle_message(whatsapp_id, message_text):
    """
    Main entry point: given the sender's whatsapp_id and what they
    just said, advance their pending registration and return the
    reply text to send back.
    """
    text = message_text.strip()

    if text.lower() == "cancel":
        delete_pending_registration(whatsapp_id)
        return "Registration cancelled. Message me again anytime to restart."

    pending = get_pending_registration(whatsapp_id)
    if pending is None:
        # Shouldn't normally happen -- webhook.py should call
        # start_registration() first for brand-new senders. Fall back
        # to starting fresh just in case.
        return start_registration(whatsapp_id)

    step = pending["step"]

    if step == "awaiting_reg_number":
        return _handle_reg_number(whatsapp_id, text)
    elif step == "awaiting_name":
        return _handle_name(whatsapp_id, text)
    elif step == "awaiting_gender":
        return _handle_gender(whatsapp_id, text)
    elif step == "awaiting_year_of_study":
        return _handle_year_of_study(whatsapp_id, text)
    elif step == "awaiting_area":
        return _handle_area(whatsapp_id, text)
    elif step == "awaiting_data_consent":
        return _handle_data_consent(whatsapp_id, text)
    elif step == "awaiting_followup_consent":
        return _handle_followup_consent(whatsapp_id, text)
    elif step == "awaiting_confirmation":
        return _handle_confirmation(whatsapp_id, text, pending)

    # Unknown step -- shouldn't happen, but fail safe rather than crash.
    delete_pending_registration(whatsapp_id)
    return "Something went wrong on my end. Let's start over -- message me again."


def _handle_reg_number(whatsapp_id, text):
    if not text:
        return "That doesn't look like a valid registration number. Please try again."

    # Returning member messaging from a new/different phone number --
    # recognize them by reg_number and just relink, skipping the rest
    # of registration entirely.
    existing = get_member_by_reg_number(text)
    if existing is not None:
        relink_whatsapp_id(text, whatsapp_id)
        delete_pending_registration(whatsapp_id)
        return (
            f"Welcome back, {existing['name']}! I've updated your number "
            "on file. You're all set."
        )

    update_pending_registration(whatsapp_id, step="awaiting_name", reg_number=text)
    return "Great. What's your full name?"


def _handle_name(whatsapp_id, text):
    if not text:
        return "Please enter your name."

    update_pending_registration(whatsapp_id, step="awaiting_gender", name=text)
    return "Thanks! What's your gender? (Reply Male, Female, or Other)"


def _handle_gender(whatsapp_id, text):
    gender = GENDER_MAP.get(text.lower())
    if gender is None:
        return "Please reply with Male, Female, or Other."

    update_pending_registration(whatsapp_id, step="awaiting_year_of_study", gender=gender)
    return "What year of study are you in? (Reply with a number, e.g. 1, 2, 3...)"


def _handle_year_of_study(whatsapp_id, text):
    if not text.isdigit() or not (1 <= int(text) <= 6):
        return "Please reply with a number between 1 and 6 for your year of study."

    update_pending_registration(
        whatsapp_id, step="awaiting_area", year_of_study=int(text)
    )
    return f"Which area do you live in?\n\n{_areas_list_text()}\n\nReply with the number."


def _handle_area(whatsapp_id, text):
    if not text.isdigit() or not (1 <= int(text) <= len(AREAS)):
        return f"Please reply with a number from 1 to {len(AREAS)}.\n\n{_areas_list_text()}"

    area = AREAS[int(text) - 1]
    update_pending_registration(whatsapp_id, step="awaiting_data_consent", area=area)
    return (
        f"Before we continue: by registering, you agree that {ORG_NAME} can "
        "store your details and use them to place you in a Bible study "
        "group, send you event updates, and track your membership status "
        "(which may be relevant for welfare support eligibility).\n\n"
        "Reply YES to continue, or NO if you'd prefer not to register."
    )


def _handle_data_consent(whatsapp_id, text):
    answer = text.lower()
    if answer not in ("yes", "no"):
        return "Please reply YES or NO."

    if answer == "no":
        delete_pending_registration(whatsapp_id)
        return "No problem -- you won't be registered. Message me anytime if you change your mind."

    update_pending_registration(whatsapp_id, step="awaiting_followup_consent", data_consent=True)
    return (
        "One more thing: would you like the bot to check in with you if "
        "you've been missing Bible study sessions -- as a gentle nudge, "
        "not a lecture?\n\n"
        "Reply YES if you'd like that kind of accountability, or NO if "
        "you'd rather not be followed up with. Either way, you'll still "
        "be fully registered."
    )


def _handle_followup_consent(whatsapp_id, text):
    answer = text.lower()
    if answer not in ("yes", "no"):
        return "Please reply YES or NO."

    followup_consent = (answer == "yes")
    update_pending_registration(
        whatsapp_id, step="awaiting_confirmation", followup_consent=followup_consent
    )

    pending = get_pending_registration(whatsapp_id)
    return _confirmation_summary_text(pending)


def _confirmation_summary_text(pending):
    return (
        "Here's what I have:\n\n"
        f"Reg No: {pending['reg_number']}\n"
        f"Name: {pending['name']}\n"
        f"Gender: {pending['gender']}\n"
        f"Year of study: {pending['year_of_study']}\n"
        f"Area: {pending['area']}\n"
        f"Follow-up check-ins: {'Yes' if pending['followup_consent'] else 'No'}\n\n"
        "Reply YES to confirm, or 'cancel' to start over."
    )


def _handle_confirmation(whatsapp_id, text, pending):
    if text.lower() != "yes":
        return "Please reply YES to confirm, or 'cancel' to start over."

    create_member(
        reg_number=pending["reg_number"],
        whatsapp_id=whatsapp_id,
        name=pending["name"],
        gender=pending["gender"],
        year_of_study=pending["year_of_study"],
        area=pending["area"],
        data_consent=True,
        followup_consent=bool(pending["followup_consent"]),
        registered_at=_now(),
    )
    delete_pending_registration(whatsapp_id)

    return f"You're all set, {pending['name']}! Welcome to {ORG_NAME}. \U0001F389"
