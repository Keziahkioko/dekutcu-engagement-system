"""
app/services/saved_messages.py

WhatsApp templates step 3 (Keziah, 2026-10-16): delivering the saved
messages. A message the bot wanted to send outside someone's 24-hour window
was saved (app/models/pending_outbound.py) and they got a "you have a new
message -- reply to read it" template. When they reply, their reply reopens
the window, so the saved messages are delivered here -- free -- oldest
first, each labelled with when it was meant to arrive.

Called from route_incoming_message (app/routes/webhook.py) on the message
worker, right after the STOP/RESUME checks, BEFORE anything else reads the
reply. What happens to their reply then:
  - a short reply ("Read it" -- the template's button -- "ok", "show me",
    even "yes") just collects the messages and goes no further. They
    hadn't seen what was waiting, so it can't be an answer to it: e.g. a
    saved number-change question "move your account? YES/NO" must not be
    answered by the "yes" they typed to READ it -- they answer once they've
    seen it;
  - a longer one (a real question) gets the saved messages first, then is
    answered as normal.
"""

from zoneinfo import ZoneInfo

from app.models.pending_outbound import take_outbound
from app.services.whatsapp_client import send_whatsapp_message, templates_enabled

_NAIROBI = ZoneInfo("Africa/Nairobi")

# Up to this many words counts as "just collecting the messages" (see the top of this file).
SHORT_REPLY_WORDS = 3

# The template's button, tapped when nothing is waiting any more (already delivered, or over a week old).
READ_IT_BUTTON = "read it"
NOTHING_WAITING_REPLY = "You're all caught up -- there's nothing waiting for you right now. 🙂"


def _label(created_at):
    when = created_at.astimezone(_NAIROBI)
    hour = when.strftime("%I").lstrip("0")
    return f"📩 Saved for you from {when.strftime('%a')} {when.day} {when.strftime('%b')}, {hour}:{when.strftime('%M%p').lower()}:"


def _format(saved):
    return f"{_label(saved['created_at'])}\n\n{saved['message_text']}"


def is_short_reply(message_text):
    return len((message_text or "").split()) <= SHORT_REPLY_WORDS


def deliver_saved_messages(sender_number, message_text):
    """
    Returns (reply, handled):
      - handled True: the reply is the whole answer -- route_incoming_message returns it and stops;
      - handled False: carry on routing the message as normal (saved ones, if any, already sent).
    """
    if not templates_enabled():   # nothing is ever saved while templates are off -- not even a database query
        return None, False
    saved = take_outbound(sender_number)
    if not saved:
        if (message_text or "").strip().lower() == READ_IT_BUTTON:
            return NOTHING_WAITING_REPLY, True
        return None, False
    texts = [_format(s) for s in saved]
    if is_short_reply(message_text):
        for text in texts[:-1]:
            send_whatsapp_message(sender_number, text)
        return texts[-1], True      # the last one goes out as the reply itself
    for text in texts:
        send_whatsapp_message(sender_number, text)
    return None, False
