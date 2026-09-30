"""
app/services/study_guides.py

Stage 14: study guides (design settled with Keziah 2026-09-30 -- see
PROJECT_LOG.md). This part: an exec leader starting a new semester's
guide. One guide per semester, normally KES 70 unless the exec subsidise
it; semesters overlap (some years in session while others are on break),
so there's no calendar -- a new guide starts when the exec say so, which
closes the previous one to new purchases while keeping all its records.

The conversation (title -> price -> confirm) expires after 30 minutes: an
abandoned one must never turn a later, unrelated message into a guide
title (the trap found in the attendance question on 2026-09-30). "cancel"
stops it at any step.
"""

import re
from datetime import datetime, timezone, timedelta

from app.models.member import get_member_by_whatsapp_id
from app.models.study_guide import (
    DEFAULT_PRICE_KES, get_current_guide, start_new_guide, count_paid_uncollected,
    get_pending_guide_creation, start_pending_guide_creation, update_pending_guide_creation,
    delete_pending_guide_creation,
)

CONVERSATION_LIFETIME = timedelta(minutes=30)
MAX_TITLE_LENGTH = 80
MAX_PRICE_KES = 5000
_CANCEL = {"cancel", "stop", "never mind", "nevermind", "forget it"}
_KEEP_DEFAULT = {"ok", "okay", "yes", "default", "same", "fine", "70"}
_YES = {"yes", "y", "yeah", "yep", "ok", "okay", "confirm", "go ahead"}
_NO = {"no", "n", "nope", "nah"}


def _now():
    return datetime.now(timezone.utc)


def begin_start_guide(member):
    """The start_study_guide intent (exec leaders only -- enforced by the intent router)."""
    start_pending_guide_creation(member["whatsapp_id"], _now().isoformat())
    current = get_current_guide()
    note = f" (This will replace '{current['title']}', which is on sale now.)" if current else ""
    return f"Let's set up the new study guide.{note}\n\nWhat's the guide's title? (Reply 'cancel' to stop.)"


def _parse_price(text):
    """'ok' -> the default; '50', 'KES 50', '50 bob', 'sh. 50' -> 50; anything else -> None."""
    cleaned = text.strip().lower().rstrip(".!")
    if cleaned in _KEEP_DEFAULT:
        return DEFAULT_PRICE_KES
    match = re.fullmatch(r"(?:kes|ksh|ksh\.|sh|sh\.|shs)?\s*(\d{1,5})\s*(?:/-|bob|shillings|kes|ksh)?", cleaned)
    if not match:
        return None
    price = int(match.group(1))
    return price if 1 <= price <= MAX_PRICE_KES else None


def handle_start_guide_message(whatsapp_id, message_text):
    """Returns the reply, or None if the conversation has expired (then the message is routed normally)."""
    pending = get_pending_guide_creation(whatsapp_id)
    if pending is None:
        return None
    if _now().replace(tzinfo=None) - pending["created_at"] > CONVERSATION_LIFETIME:
        delete_pending_guide_creation(whatsapp_id)
        return None

    text = message_text.strip()
    lowered = text.lower().rstrip(".!")
    if lowered in _CANCEL:
        delete_pending_guide_creation(whatsapp_id)
        return "Okay -- no new study guide was started."

    if pending["step"] == "awaiting_title":
        if not text or len(text) > MAX_TITLE_LENGTH:
            return f"Please send a shorter title (up to {MAX_TITLE_LENGTH} characters), or 'cancel'."
        update_pending_guide_creation(whatsapp_id, "awaiting_price", title=text)
        return (f"Price? It's KES {DEFAULT_PRICE_KES} unless the exec are subsidising it -- reply 'ok' for "
                f"KES {DEFAULT_PRICE_KES}, or send a different amount (e.g. 50).")

    if pending["step"] == "awaiting_price":
        price = _parse_price(text)
        if price is None:
            return (f"Please reply 'ok' for KES {DEFAULT_PRICE_KES}, or an amount in shillings between 1 and "
                    f"{MAX_PRICE_KES} (e.g. 50) -- or 'cancel'.")
        update_pending_guide_creation(whatsapp_id, "awaiting_confirm", price_kes=price)
        current = get_current_guide()
        closing = (f" This closes '{current['title']}' to new purchases -- its records are kept, and anyone "
                   "who has paid for it but not collected stays on their leader's list.") if current else ""
        return f"Start '{pending['title']}' at KES {price}?{closing}\n\nReply YES to confirm, or NO to cancel."

    if pending["step"] == "awaiting_confirm":
        if lowered in _NO:
            delete_pending_guide_creation(whatsapp_id)
            return "Okay -- no new study guide was started."
        if lowered not in _YES:
            return "Please reply YES to start it, or NO to cancel."
        member = get_member_by_whatsapp_id(whatsapp_id)
        new, previous = start_new_guide(pending["title"], pending["price_kes"],
                                        member["reg_number"] if member else None, _now().isoformat())
        delete_pending_guide_creation(whatsapp_id)
        reply = f"Done -- '{new['title']}' is now on sale at KES {new['price_kes']}."
        if previous:
            waiting = count_paid_uncollected(previous["id"])
            reply += f" '{previous['title']}' is closed to new purchases"
            if waiting == 1:
                reply += "; 1 paid copy is still to be handed over and stays on its leader's list."
            elif waiting:
                reply += f"; {waiting} paid copies are still to be handed over and stay on leaders' lists."
            else:
                reply += "."
        return reply

    delete_pending_guide_creation(whatsapp_id)   # unknown step -- never trap the leader
    return None
