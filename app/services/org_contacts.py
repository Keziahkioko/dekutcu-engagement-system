"""
app/services/org_contacts.py

DeKUTCU's OFFICIAL, published contact details -- the ones already on
posters (the secretary's number and the CU email) -- which the bot may
freely give out. Distinct from leaders' personal numbers, which are
never shared with members (leaders never agreed to that; a notified
leader reaches out instead). Settled with Keziah after the first live
test, where the groups feature INVENTED an office, an email and a phone
number rather than admit it had none.

Read from configuration (.env locally, Render's Environment settings in
production), not hard-coded: the secretary changes every spiritual year
at the AGM, so updating the number is a settings change, not a code
change.
    ORG_CONTACT_PHONE        e.g. +2547XXXXXXXX
    ORG_CONTACT_PHONE_LABEL  e.g. "the CU Secretary" (default)
    ORG_CONTACT_EMAIL        e.g. dekutcu@students.dkut.ac.ke
Anything unset simply isn't offered -- the bot says it doesn't have it,
never guesses.

The reply is built here directly -- no language model involved -- so it
can't invent or garble a contact detail.
"""

import os
import re


def get_official_contacts():
    phone = os.getenv("ORG_CONTACT_PHONE", "").strip()
    email = os.getenv("ORG_CONTACT_EMAIL", "").strip()
    label = os.getenv("ORG_CONTACT_PHONE_LABEL", "").strip() or "the CU Secretary"
    return {"phone": phone or None, "phone_label": label, "email": email or None}


def contact_reply():
    contacts = get_official_contacts()
    lines = []
    if contacts["phone"]:
        lines.append(f"Phone ({contacts['phone_label']}): {contacts['phone']}")
    if contacts["email"]:
        lines.append(f"Email: {contacts['email']}")
    if not lines:
        return (
            "I don't have DeKUTCU's official contact details yet, sorry. If you'd like "
            "to talk to someone, just say \"I'd like to talk to a leader\" and I'll let one know."
        )
    return (
        "You can reach DeKUTCU here:\n" + "\n".join(lines)
        + "\n\nOr just say \"I'd like to talk to a leader\" and I'll let one know."
    )


def is_official(value):
    """True if `value` is one of the configured official contacts (phone compared by digits only)."""
    contacts = get_official_contacts()
    if contacts["email"] and value.strip().lower() == contacts["email"].lower():
        return True
    digits = re.sub(r"\D", "", value)
    return bool(contacts["phone"]) and digits != "" and digits == re.sub(r"\D", "", contacts["phone"])
