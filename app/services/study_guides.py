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

from app.models.member import get_member_by_whatsapp_id, get_member_by_reg_number, get_exec_office_holders
from app.models.study_guide import (
    DEFAULT_PRICE_KES, get_current_guide, start_new_guide, count_paid_uncollected,
    get_pending_guide_creation, start_pending_guide_creation, update_pending_guide_creation,
    delete_pending_guide_creation,
    get_guide, get_group_leader, get_paid_purchase, get_recent_pending_purchase,
    get_pending_guide_purchase, start_pending_guide_purchase, delete_pending_guide_purchase,
)
from app.services import mpesa, guide_payments
from app.services.whatsapp_client import send_whatsapp_message

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


# ---------------------------------------------------------------------
# Step 3: a member buying the current guide. The M-Pesa side is in
# guide_payments.py (a payment only counts once Safaricom confirms it);
# this is the conversation around it and the messages about the result.
# ---------------------------------------------------------------------

PROMPT_GUARD = timedelta(minutes=3)          # no second prompt while one may still be on their phone
PHONE_QUESTION_LIFETIME = timedelta(minutes=15)
DIRECTOR_OFFICE = "Discipleship Ministry Director"


def _display_phone(phone):
    """'254712345678' -> '0712 345 678' -- how Kenyans write their own number."""
    local = "0" + phone[3:] if phone and phone.startswith("254") else (phone or "")
    return f"{local[:4]} {local[4:7]} {local[7:]}" if len(local) == 10 else local


def find_collector(member):
    """
    Who this member collects their printed copy from (Keziah's collection chain): their group
    leader; otherwise the Discipleship Ministry Director; otherwise the Discipleship team.
    Returns (leader_row_or_None, how to describe them). NAME only -- a leader's personal
    number is never given to members.
    """
    leader = get_group_leader(member["group_label"])
    if leader and leader["reg_number"] != member["reg_number"]:
        return leader, f"{leader['name']}, your group leader"
    director = get_exec_office_holders().get(DIRECTOR_OFFICE)
    if director and director["reg_number"] != member["reg_number"]:
        return director, f"{director['name']}, the Discipleship Ministry Director"
    return None, "the Discipleship team"


def begin_purchase(member):
    """The purchase_study_guide intent."""
    guide = get_current_guide()
    if not guide:
        return "There's no study guide on sale right now -- I'll have it here as soon as the next one is out."
    if not mpesa.is_configured():
        return "Study guide payments aren't switched on yet -- please check with your group leader for now."

    paid = get_paid_purchase(guide["id"], member["reg_number"])
    if paid:
        code = f" (M-Pesa code {paid['mpesa_receipt']})" if paid["mpesa_receipt"] else ""
        if paid["collected_at"]:
            return f"You've already bought '{guide['title']}'{code}, and you collected it on {paid['collected_at']:%d %b}."
        _, collector = find_collector(member)
        return f"You've already bought '{guide['title']}'{code}. Collect your copy from {collector}."

    recent = get_recent_pending_purchase(guide["id"], member["reg_number"], _now().replace(tzinfo=None) - PROMPT_GUARD)
    if recent:
        return (f"I've already sent an M-Pesa prompt to {_display_phone(recent['phone'])} -- please check your phone. "
                "If it's gone, wait a couple of minutes and ask me again.")

    start_pending_guide_purchase(member["whatsapp_id"], guide["id"], _now().isoformat())
    own = mpesa.normalise_phone(member["whatsapp_id"])
    intro = f"This semester's study guide is '{guide['title']}' -- KES {guide['price_kes']}."
    if own:
        return (f"{intro} Shall I send the M-Pesa prompt to {_display_phone(own)} (your WhatsApp number)?\n\n"
                "Reply YES, or send a different Safaricom number -- or 'cancel'.")
    return f"{intro} Which Safaricom number should I send the M-Pesa prompt to? (Or reply 'cancel'.)"


def handle_purchase_reply(whatsapp_id, message_text):
    """
    The reply to "which number?". Returns the reply, or None -- the question is dropped and the
    message routed normally -- if it's expired or the message isn't an answer (never traps them).
    """
    pending = get_pending_guide_purchase(whatsapp_id)
    if pending is None:
        return None
    if _now().replace(tzinfo=None) - pending["created_at"] > PHONE_QUESTION_LIFETIME:
        delete_pending_guide_purchase(whatsapp_id)
        return None

    text = message_text.strip()
    lowered = text.lower().rstrip(".!")
    if lowered in _CANCEL or lowered in _NO:
        delete_pending_guide_purchase(whatsapp_id)
        return "Okay -- no payment was started. Ask me anytime if you'd like to buy the guide."

    if lowered in _YES:
        phone = mpesa.normalise_phone(whatsapp_id)
        if not phone:
            return "Which Safaricom number should I send the M-Pesa prompt to? (Or reply 'cancel'.)"
    elif re.fullmatch(r"[+\d\s\-()]{9,20}", text):
        phone = mpesa.normalise_phone(text)
        if not phone:
            return ("That doesn't look like a Kenyan mobile number -- please send it like 0712 345 678, "
                    "or reply 'cancel'.")
    else:
        delete_pending_guide_purchase(whatsapp_id)
        return None

    delete_pending_guide_purchase(whatsapp_id)
    member = get_member_by_whatsapp_id(whatsapp_id)
    guide = get_guide(pending["guide_id"])
    current = get_current_guide()
    if not member or not guide or not current or current["id"] != guide["id"]:
        return "The study guide on sale has just changed -- please ask me again to buy it."
    if get_paid_purchase(guide["id"], member["reg_number"]):
        return f"You've already bought '{guide['title']}' -- no need to pay again."
    try:
        guide_payments.start_payment(member, guide, phone)
    except mpesa.MpesaError:
        return "I couldn't send the M-Pesa prompt just now -- please try again in a few minutes."
    return (f"I've sent an M-Pesa prompt for KES {guide['price_kes']} to {_display_phone(phone)} -- enter your "
            "M-Pesa PIN on your phone to pay. I'll confirm here once it's through.")


def notify_purchase_result(purchase):
    """
    The member's message once a payment's result is FINAL (called exactly once per purchase by
    guide_payments). Sent directly -- it arrives in its own turn, not as a reply.
    """
    member = get_member_by_reg_number(purchase["reg_number"])
    if not member or not member["whatsapp_id"]:
        return   # withdrew meanwhile, or no WhatsApp on file
    guide = get_guide(purchase["guide_id"])
    if purchase["status"] == "paid":
        _, collector = find_collector(member)
        code = (f"M-Pesa code: {purchase['mpesa_receipt']}." if purchase["mpesa_receipt"]
                else "Your M-Pesa confirmation SMS has the transaction code.")
        text = (f"Payment received ✅ -- '{guide['title']}', KES {purchase['amount_kes']}. {code}\n\n"
                f"Collect your copy from {collector}.")
    elif purchase["status"] == "duplicate":
        code = f" (code {purchase['mpesa_receipt']})" if purchase["mpesa_receipt"] else ""
        text = (f"It looks like you paid twice for '{guide['title']}'. You already have a paid copy, so this "
                f"second payment{code} has been recorded so it can be refunded.")
    else:
        reason = mpesa.describe(purchase["result_code"]) if purchase["result_code"] is not None else "no result came back"
        text = (f"Your payment for '{guide['title']}' didn't go through -- {reason}. "
                "Say 'buy the study guide' to try again.")
    send_whatsapp_message(member["whatsapp_id"], text)
