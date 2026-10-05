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
  - "cancel" is a universal escape hatch at any step -- always a full
    restart. At the final review step specifically, naming a field
    (e.g. "year of study") corrects just that one field without
    losing everything else already collected.
  - Recognizing an off-topic question mid-registration and answering
    it (rather than swallowing it as a form answer) is intent-router
    territory (Stage 4) and out of scope here.
"""

import re
from datetime import datetime, timezone

from app.models.member import (
    get_member_by_whatsapp_id,
    get_member_by_reg_number,
    create_member,
    relink_whatsapp_id,
)
from app.services import conversation
from app.services import number_change
from app.models.pending_registration import (
    get_pending_registration,
    start_pending_registration,
    update_pending_registration,
    delete_pending_registration,
)

ORG_NAME = "DeKUT CU"  # adjust to the organization's actual name/branding
REG_NUMBER_EXAMPLE = "C026-01-0735/2023"  # example format shown to members

AREAS = [
    "Bomas", "Internal Hostels", "Nyeri View", "Catholic Hostels",
    "Nyaribo", "Embassy", "King'ong'o", "Nyeri Town", "Gate A", "Kahawa",
]

GENDER_MAP = {
    "m": "Male", "male": "Male",
    "f": "Female", "female": "Female",
}

# Maps keywords a member might type at the confirmation step (to request
# a correction) to the step that field belongs to and the question to
# re-ask. Checked as substrings against the lowercased reply.
CORRECTION_FIELDS = [
    (("reg", "registration"), "awaiting_reg_number"),
    (("name",), "awaiting_name"),
    (("gender",), "awaiting_gender"),
    (("year",), "awaiting_year_of_study"),
    (("area",), "awaiting_area"),
    (("follow",), "awaiting_followup_consent"),
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _areas_list_text():
    lines = [f"{i + 1}. {area}" for i, area in enumerate(AREAS)]
    return "\n".join(lines)


def _reg_number_question():
    return f"What's your school registration number?\n(e.g. {REG_NUMBER_EXAMPLE})"


def _name_question():
    return "What's your full name?"


def _gender_question():
    return "What's your gender? (Reply Male or Female)"


def _year_question():
    return "What year of study are you in? (Reply with a number, e.g. 1, 2, 3...)"


def _area_question():
    return (
        f"Which area do you live in?\n\n{_areas_list_text()}\n\n"
        "Reply with the number. If your area isn't listed, choose the one closest to you."
    )


def _followup_question():
    return (
        "Would you like the bot to check in with you if you've been missing "
        "Bible study, fellowships, or other events? This helps us support "
        "and stay connected with you.\n\n"
        "Reply YES if you'd like these check-ins, or NO if you'd rather not. "
        "Either way, you'll still be fully registered."
    )


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
        "with your Bible study group, fellowships, events, and updates.\n\n"
        "To get started, I'll need a few quick details -- please make sure "
        "they're accurate, since they're used to place you correctly and "
        "confirm your membership status.\n\n"
        "(You can reply 'cancel' at any point to stop.)\n\n"
        f"{_reg_number_question()}"
    )


def handle_message(whatsapp_id, message_text):
    """
    Main entry point: given the sender's whatsapp_id and what they
    just said, advance their pending registration and return the
    reply text to send back.
    """
    text = message_text.strip()

    if conversation.is_cancel(text):
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
        return _handle_reg_number(whatsapp_id, text, pending)
    elif step == "awaiting_name":
        return _handle_name(whatsapp_id, text, pending)
    elif step == "awaiting_gender":
        return _handle_gender(whatsapp_id, text, pending)
    elif step == "awaiting_year_of_study":
        return _handle_year_of_study(whatsapp_id, text, pending)
    elif step == "awaiting_area":
        return _handle_area(whatsapp_id, text, pending)
    elif step == "awaiting_data_consent":
        return _handle_data_consent(whatsapp_id, text, pending)
    elif step == "awaiting_followup_consent":
        return _handle_followup_consent(whatsapp_id, text, pending)
    elif step == "awaiting_confirmation":
        return _handle_confirmation(whatsapp_id, text, pending)

    # Unknown step -- shouldn't happen, but fail safe rather than crash.
    delete_pending_registration(whatsapp_id)
    return "Something went wrong on my end. Let's start over -- message me again."


def _advance(whatsapp_id, pending, field_updates, next_step_if_normal, normal_reply):
    """
    Shared logic every field handler uses after validating an answer:
    if this answer was collected as a CORRECTION (member named this
    field from the confirmation screen), save it and jump straight
    back to the confirmation summary instead of continuing the
    normal linear sequence.
    """
    if pending["correcting"]:
        update_pending_registration(
            whatsapp_id, step="awaiting_confirmation", correcting=False, **field_updates
        )
        updated = get_pending_registration(whatsapp_id)
        return _confirmation_summary_text(updated)

    update_pending_registration(whatsapp_id, step=next_step_if_normal, **field_updates)
    return normal_reply


def normalise_reg_number(text):
    """'c026 -01-0735/2023' -> 'C026-01-0735/2023' -- one student, one form (QA 2026-10-01: typed variants made duplicate accounts)."""
    return re.sub(r"\s+", "", (text or "").upper())


def _looks_like_reg_number(reg):
    """A plausible DeKUT registration number: letters/digits with '-' or '/', at least 4 digits, and a '/YEAR'-style part."""
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9/\-]{5,24}", reg)) and sum(c.isdigit() for c in reg) >= 4 and "/" in reg


def _handle_reg_number(whatsapp_id, text, pending):
    if conversation.looks_like_new_request(text) or conversation.is_help_request(text):
        # QA 2026-10-01: a question here used to be saved AS the registration number.
        return ("I'll be able to help with that once you're registered -- it only takes a minute.\n\n"
                f"{_reg_number_question()}\n(Or reply 'cancel' to stop.)")
    text = normalise_reg_number(text)
    if not _looks_like_reg_number(text):
        return f"That doesn't look like a registration number. Please send it like this: {REG_NUMBER_EXAMPLE}"

    # A registration number that already belongs to a member: possibly them on
    # a new phone -- but reg numbers aren't secret, so this used to let anyone
    # take over anyone's account (QA 2026-10-01, BUG-02). Nothing moves until
    # the old number or a leader confirms it -- see number_change.py.
    existing = get_member_by_reg_number(text)
    if existing is not None:
        delete_pending_registration(whatsapp_id)
        return number_change.request_move(existing, whatsapp_id)

    return _advance(
        whatsapp_id, pending,
        field_updates={"reg_number": text},
        next_step_if_normal="awaiting_name",
        normal_reply=_name_question(),
    )


def _handle_name(whatsapp_id, text, pending):
    # QA 2026-10-01: anything was accepted as a name -- a question, "hello", 300 characters.
    if "?" in text:
        return "I'll answer that once you're registered! For now -- what's your full name?"
    letters = sum(c.isalpha() for c in text)
    if letters < 2 or len(text) > 60 or any(c.isdigit() for c in text):
        return "Please send your full name as it appears on your student ID (letters only, e.g. Mary Wanjiru)."
    if conversation.normalise(text) in {"hi", "hello", "hey", "sasa", "niaje", "yes", "no", "ok", "okay"}:
        return "What's your full name? (e.g. Mary Wanjiru)"

    return _advance(
        whatsapp_id, pending,
        field_updates={"name": text},
        next_step_if_normal="awaiting_gender",
        normal_reply=_gender_question(),
    )


def _handle_gender(whatsapp_id, text, pending):
    gender = GENDER_MAP.get(text.lower())
    if gender is None:
        return "Please reply with Male or Female."

    return _advance(
        whatsapp_id, pending,
        field_updates={"gender": gender},
        next_step_if_normal="awaiting_year_of_study",
        normal_reply=_year_question(),
    )


def _handle_year_of_study(whatsapp_id, text, pending):
    if not text.isdigit() or not (1 <= int(text) <= 6):
        return "Please reply with a number between 1 and 6 for your year of study."

    return _advance(
        whatsapp_id, pending,
        field_updates={"year_of_study": int(text)},
        next_step_if_normal="awaiting_area",
        normal_reply=_area_question(),
    )


def _resolve_area(text):
    """
    Accepts either a number (matching the numbered list) or the area
    name typed directly, matched case-insensitively. Returns the
    canonical area name, or None if nothing matched.
    """
    if text.isdigit() and 1 <= int(text) <= len(AREAS):
        return AREAS[int(text) - 1]

    lowered = text.strip().lower()
    for candidate in AREAS:
        if candidate.lower() == lowered:
            return candidate

    return None


def _handle_area(whatsapp_id, text, pending):
    area = _resolve_area(text)
    if area is None:
        return f"Please reply with a number from 1 to {len(AREAS)}, or type your area's name.\n\n{_area_question()}"

    if pending["correcting"]:
        return _advance(
            whatsapp_id, pending,
            field_updates={"area": area},
            next_step_if_normal="awaiting_area",  # unused when correcting
            normal_reply="",  # unused when correcting
        )

    update_pending_registration(whatsapp_id, step="awaiting_data_consent", area=area)
    return (
        f"Before we continue: by registering, you agree that {ORG_NAME} can "
        "store your details and use them to place you in a Bible study "
        "group, send you event updates, and track your membership status "
        "(which may be relevant for welfare support eligibility).\n\n"
        "Reply YES to continue, or NO if you'd prefer not to register."
    )


def _handle_data_consent(whatsapp_id, text, pending):
    answer = conversation.strict_yes_no(text)
    if answer is None:
        return "Please reply YES or NO."

    if answer == "no":
        delete_pending_registration(whatsapp_id)
        return "No problem -- you won't be registered. Message me anytime if you change your mind."

    update_pending_registration(whatsapp_id, step="awaiting_followup_consent", data_consent=True)
    return _followup_question()


def _handle_followup_consent(whatsapp_id, text, pending):
    answer = conversation.strict_yes_no(text)
    if answer is None:
        return "Please reply YES or NO."

    followup_consent = (answer == "yes")
    update_pending_registration(
        whatsapp_id, step="awaiting_confirmation", followup_consent=followup_consent, correcting=False
    )

    updated = get_pending_registration(whatsapp_id)
    return _confirmation_summary_text(updated)


def _confirmation_summary_text(pending):
    return (
        "Here's what I have:\n\n"
        f"Reg No: {pending['reg_number']}\n"
        f"Name: {pending['name']}\n"
        f"Gender: {pending['gender']}\n"
        f"Year of study: {pending['year_of_study']}\n"
        f"Area: {pending['area']}\n"
        f"Follow-up check-ins: {'Yes' if pending['followup_consent'] else 'No'}\n\n"
        "Reply YES to confirm, tell me what to fix (e.g. 'name' or 'area'), "
        "or 'cancel' to start over."
    )


def _handle_confirmation(whatsapp_id, text, pending):
    lowered = text.strip().lower()

    if conversation.strict_yes_no(text) == "yes":
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

    # Field-correction request: they named a field instead of saying YES.
    question_by_step = {
        "awaiting_reg_number": _reg_number_question(),
        "awaiting_name": _name_question(),
        "awaiting_gender": _gender_question(),
        "awaiting_year_of_study": _year_question(),
        "awaiting_area": _area_question(),
        "awaiting_followup_consent": _followup_question(),
    }
    for keywords, step in CORRECTION_FIELDS:
        if any(kw in lowered for kw in keywords):
            update_pending_registration(whatsapp_id, step=step, correcting=True)
            return f"No problem, let's fix that.\n\n{question_by_step[step]}"

    return (
        "Please reply YES to confirm, tell me which detail to fix "
        "(e.g. 'year of study'), or 'cancel' to start over."
    )