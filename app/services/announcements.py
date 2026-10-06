"""
app/services/announcements.py

Leaders announcing things to members (2026-10-02). Found by Keziah: a
leader naturally says "I want to announce to the members" -- which the AI
correctly labelled send_announcement, a placeholder -- while the working
feature hid behind the words "create an event". Now "announce" is the way
in, and the bot asks what kind it is:

  exec leader:  "On a date, or just a message?"
                  on a date     -> the existing event steps (event_manager)
                  just a message -> who gets it (everyone, or one area --
                                    Keziah's choice) -> the message -> a
                                    preview -> YES sends it
  group leader: always a message, always to their own group (group leaders
                don't create events).

Recipients: every registered member in the audience with a WhatsApp number
-- including members who switched off check-ins: an announcement isn't a
follow-up, the same rule as broadcast events. The sender isn't sent their
own announcement. Sending runs in the background (hundreds of messages take
minutes) and the leader is told how many it reached. Every announcement is
recorded in `announcements` (who, audience, text, how many, when).

Same conversation rules as every other flow: "cancel" always gets out, a
question at a choice step is handled normally (webhook.py), and it expires
after 30 minutes. NOTE for rollout: outside WhatsApp's 24-hour window these
need an approved template -- see PROJECT_LOG.md (Meta's 24-hour rule).
"""

import re
import threading
from datetime import datetime, timezone, timedelta

from app.database import get_connection
from app.models.member import get_member_by_whatsapp_id
from app.services import conversation, event_manager
from app.services.registration import AREAS
from app.services.whatsapp_client import send_whatsapp_message

CONVERSATION_LIFETIME = timedelta(minutes=30)
MAX_LENGTH = 1000


def init_announcement_tables():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_announcement (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            audience TEXT,
            message TEXT,
            created_at TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id SERIAL PRIMARY KEY,
            sent_by_reg_number TEXT,
            audience TEXT NOT NULL,
            message TEXT NOT NULL,
            recipients INTEGER NOT NULL,
            sent_at TIMESTAMP NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def _q(sql, params=(), one=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall() if cursor.description else None
    conn.commit()
    cursor.close()
    conn.close()
    return (rows[0] if rows else None) if one else rows


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_pending_announcement(whatsapp_id):
    return _q("SELECT * FROM pending_announcement WHERE whatsapp_id = %s", (whatsapp_id,), one=True)


def _set_pending(whatsapp_id, step, audience=None, message=None):
    _q("""INSERT INTO pending_announcement (whatsapp_id, step, audience, message, created_at)
          VALUES (%s, %s, %s, %s, %s)
          ON CONFLICT (whatsapp_id) DO UPDATE SET step = EXCLUDED.step,
              audience = COALESCE(EXCLUDED.audience, pending_announcement.audience),
              message = COALESCE(EXCLUDED.message, pending_announcement.message)""",
       (whatsapp_id, step, audience, message, _now()))


def delete_pending_announcement(whatsapp_id):
    _q("DELETE FROM pending_announcement WHERE whatsapp_id = %s", (whatsapp_id,))


# --- audiences -----------------------------------------------------------------------------------

def _recipients(audience, sender_reg_number):
    """Registered members with WhatsApp in the audience ('all', 'area:<name>', 'group:<label>'), never the sender."""
    where, params = "data_consent AND whatsapp_id IS NOT NULL AND reg_number <> %s", [sender_reg_number]
    if audience.startswith("area:"):
        where += " AND area = %s"
        params.append(audience[5:])
    elif audience.startswith("group:"):
        where += " AND group_label = %s"
        params.append(audience[6:])
    return _q(f"SELECT whatsapp_id FROM members WHERE {where}", params)


def _describe(audience):
    if audience.startswith("area:"):
        return f"members in {audience[5:]}"
    if audience.startswith("group:"):
        return f"your group, {audience[6:]}"
    return "all members"


def _audience_question(member):
    total = len(_recipients("all", member["reg_number"]))
    areas = "\n".join(f"{i + 2}. {area}" for i, area in enumerate(AREAS))
    return (f"Who should get it?\n\n1. Everyone ({total} members)\n{areas}\n\n"
            "Reply with the number (or 'cancel').")


def _parse_audience(text):
    n = conversation.normalise(text)
    if n in ("1", "everyone", "all", "all members", "everybody"):
        return "all"
    if n.isdigit() and 2 <= int(n) <= len(AREAS) + 1:
        return "area:" + AREAS[int(n) - 2]
    for area in AREAS:
        if n == area.lower():
            return "area:" + area
    return None


# --- the conversation ----------------------------------------------------------------------------

def begin(member):
    """The send_announcement intent: exec leaders and group leaders (the router lets both through)."""
    whatsapp_id = member["whatsapp_id"]
    if not member["is_leader"]:
        _set_pending(whatsapp_id, "awaiting_message", audience=f"group:{member['leads_group_label']}")
        return (f"Sure -- what would you like to tell your group ({member['leads_group_label']})? "
                "Type the message (or 'cancel').")
    _set_pending(whatsapp_id, "awaiting_kind")
    return ("Happy to send that out. Is it:\n\n"
            "1. Something happening on a date (members can RSVP or plan for it)\n"
            "2. Just a message (a notice, a change, a reminder)\n\n"
            "Reply 1 or 2 (or 'cancel').")


def handle_message(whatsapp_id, message_text):
    """Returns the reply, or None if the conversation expired (then the message is handled normally)."""
    pending = get_pending_announcement(whatsapp_id)
    if pending is None:
        return None
    if _now() - pending["created_at"] > CONVERSATION_LIFETIME:
        delete_pending_announcement(whatsapp_id)
        return None
    member = get_member_by_whatsapp_id(whatsapp_id)
    text = message_text.strip()
    step = pending["step"]

    if step == "awaiting_kind":
        # Whole words only: "update everyone" must not read as "date".
        n = conversation.normalise(text)
        if n in ("1", "one", "first", "the first one") or re.search(r"\b(date|dated|event|events)\b", n):
            delete_pending_announcement(whatsapp_id)
            return event_manager.start_create_event(whatsapp_id)
        if n in ("2", "two", "second", "the second one", "notice", "a notice") or re.search(r"\b(message|announcement)\b", n):
            _set_pending(whatsapp_id, "awaiting_audience")
            return _audience_question(member)
        return "Please reply 1 (something on a date) or 2 (just a message) -- or 'cancel'."

    if step == "awaiting_audience":
        audience = _parse_audience(text)
        if audience is None:
            return f"Please reply with a number from 1 to {len(AREAS) + 1}.\n\n" + _audience_question(member)
        _set_pending(whatsapp_id, "awaiting_message", audience=audience)
        return f"Okay -- {_describe(audience)}. Type the message you'd like to send (or 'cancel')."

    if step == "awaiting_message":
        if not text:
            return "Please type the message you'd like to send (or 'cancel')."
        if len(text) > MAX_LENGTH:
            return f"That's a bit long for WhatsApp -- please keep it under {MAX_LENGTH} characters."
        count = len(_recipients(pending["audience"], member["reg_number"]))
        if count == 0:
            delete_pending_announcement(whatsapp_id)
            return f"There's nobody in {_describe(pending['audience'])} I can message yet, so nothing was sent."
        _set_pending(whatsapp_id, "awaiting_confirm", message=text)
        return (f"Here's how it will look:\n\n{format_announcement(text, member)}\n\n"
                f"Send this to {count} {'member' if count == 1 else 'members'} ({_describe(pending['audience'])})? "
                "Reply YES to send, or NO to cancel.")

    if step == "awaiting_confirm":
        answer = conversation.yes_no(text)
        if answer == "no":
            delete_pending_announcement(whatsapp_id)
            return "Okay -- nothing was sent."
        if answer != "yes":
            return "Please reply YES to send it, or NO to cancel."
        delete_pending_announcement(whatsapp_id)
        threading.Thread(target=_send, args=(member, pending["audience"], pending["message"]), daemon=True).start()
        return "Sending it now -- I'll let you know once it's gone out."

    delete_pending_announcement(whatsapp_id)
    return None


def format_announcement(text, sender):
    first = (sender["name"] or "").split()[0] if sender and sender["name"] else "DeKUTCU"
    return f"📢 *DeKUTCU announcement*\n\n{text}\n\n-- {first}"


def _send(member, audience, text):
    """Background: sends to everyone in the audience, records it, tells the leader how many it reached."""
    body = format_announcement(text, member)
    sent = 0
    for row in _recipients(audience, member["reg_number"]):
        try:
            response = send_whatsapp_message(row["whatsapp_id"], body)
            if getattr(response, "status_code", 200) == 200:
                sent += 1
        except Exception as e:
            print(f"Announcement to one member failed: {e}")
    _q("""INSERT INTO announcements (sent_by_reg_number, audience, message, recipients, sent_at)
          VALUES (%s, %s, %s, %s, %s)""", (member["reg_number"], audience, text, sent, _now()))
    send_whatsapp_message(member["whatsapp_id"],
                          f"Your announcement went out to {sent} {'member' if sent == 1 else 'members'} ({_describe(audience)}).")
