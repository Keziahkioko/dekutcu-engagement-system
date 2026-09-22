"""
app/services/whatsapp_client.py

Sends outbound WhatsApp messages via Meta's Graph API. Pulled out of
app/routes/webhook.py so it can also be called from a background
thread (e.g. the Stage 5 allocation engine's follow-up message) --
services/intent_router.py can't import from routes/webhook.py without
creating a circular import, since webhook.py already imports from
intent_router.py.
"""

import os
import requests

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")


def send_whatsapp_message(to_number, message_text):
    """
    Sends a WhatsApp message using Meta's API.
    """
    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text},
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code == 200:
        print(f"Message sent successfully to {to_number}")
    else:
        print(f"Failed to send message. Status: {response.status_code}, Response: {response.text}")

    return response
