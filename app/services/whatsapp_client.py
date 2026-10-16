"""
app/services/whatsapp_client.py

Sends outbound WhatsApp messages via Meta's Graph API. Pulled out of
app/routes/webhook.py so it can also be called from a background
thread (e.g. the Stage 5 allocation engine's follow-up message) --
services/intent_router.py can't import from routes/webhook.py without
creating a circular import, since webhook.py already imports from
intent_router.py.

WhatsApp templates (Keziah, 2026-10-15/16): Meta allows an ordinary message
only within 24 hours of the person's last message to us (the bot uses 23 --
app/models/last_inbound.py). The check lives HERE, inside
send_whatsapp_message, so none of the ~45 places that send can slip past it:
  - inside the window: sent as normal (free);
  - outside it: the real message is saved (app/models/pending_outbound.py)
    and the person gets ONE "message waiting" template -- delivered when
    they reply. A caller can say what it's about (topic="a study guide
    update"); otherwise "an update".
All of this is behind WHATSAPP_TEMPLATES_ENABLED (off unless "true"): until
Meta approves the templates, everything is sent exactly as before.
"""

import os
import requests

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

# The template as it will be submitted to Meta (utility category, English):
#   "Hi {{1}}, you have a new message from DeKUTCU about {{2}}. Reply to this message to read it."  [Read it]
MESSAGE_WAITING_TEMPLATE = "dekutcu_message_waiting"
DEFAULT_TOPIC = "an update"
_LANGUAGE = "en"


def templates_enabled():
    return os.getenv("WHATSAPP_TEMPLATES_ENABLED", "").strip().lower() == "true"


def _post(payload):
    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    return requests.post(url, headers=headers, json={"messaging_product": "whatsapp", **payload})


def _send_text(to_number, message_text):
    response = _post({"to": to_number, "type": "text", "text": {"body": message_text}})
    if response.status_code == 200:
        print(f"Message sent successfully to {to_number}")
    else:
        print(f"Failed to send message. Status: {response.status_code}, Response: {response.text}")
    return response


def send_whatsapp_template(to_number, template_name, values):
    """One approved template; `values` fill its {{1}}, {{2}}... in order."""
    response = _post({
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": _LANGUAGE},
            "components": [{"type": "body",
                            "parameters": [{"type": "text", "text": str(v)} for v in values]}],
        },
    })
    if response.status_code == 200:
        print(f"Template '{template_name}' sent to {to_number}")
    else:
        print(f"Failed to send template '{template_name}'. Status: {response.status_code}, Response: {response.text}")
    return response


class _Held:
    """
    What callers get back when the message was saved and a template already covers it: treated as sent
    (status 200) -- it WILL reach them, as soon as they reply.
    """
    status_code = 200
    text = "saved until they reply (a template is already waiting)"


def _first_name(to_number):
    from app.models.member import get_member_by_whatsapp_id
    member = get_member_by_whatsapp_id(to_number)
    name = ((member or {}).get("name") or "").strip()
    return name.split()[0] if name else "there"


def _hold_until_they_reply(to_number, message_text, topic):
    from app.models.pending_outbound import save_outbound, mark_notified
    if save_outbound(to_number, message_text):
        return _Held()
    response = send_whatsapp_template(to_number, MESSAGE_WAITING_TEMPLATE,
                                      [_first_name(to_number), topic or DEFAULT_TOPIC])
    if response.status_code == 200:
        mark_notified(to_number)
    # If Meta refused (e.g. a blocked number), the message stays saved -- they still get it if they ever
    # write -- and the failure is logged above; nothing retries in a loop.
    return response


def send_whatsapp_message(to_number, message_text, topic=None):
    """
    Sends a WhatsApp message using Meta's API -- or, outside the person's 24-hour window (with templates
    switched on), saves it and sends a "message waiting" template instead. See the top of this file.
    """
    if not templates_enabled():
        return _send_text(to_number, message_text)
    from app.models.last_inbound import within_window
    try:
        inside = within_window(to_number)
    except Exception as e:
        # Can't tell (database trouble): try the ordinary message -- the worst case is Meta refusing it.
        print(f"Couldn't check the 24-hour window for {to_number} ({e}); sending normally")
        inside = True
    if inside:
        return _send_text(to_number, message_text)
    return _hold_until_they_reply(to_number, message_text, topic)
