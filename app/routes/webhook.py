"""
app/routes/webhook.py

STAGE 1 -- the skeleton webhook (now a Blueprint, registered onto the
app inside app/__init__.py instead of running standalone).

What this file does, in plain terms:
1. Meta needs to "verify" our webhook URL once, by sending a special
   GET request with a secret code. We must reply with that same code
   back, or Meta will refuse to ever send us real messages.
2. After that, every time someone messages our WhatsApp number, Meta
   sends a POST request to this same URL, containing what they said.
3. We read the message out of that POST request, and (for now) just
   reply with a fixed test message, to prove the whole chain works.
"""

import os
import requests
from flask import Blueprint, request, jsonify

from app.models.pending_registration import get_pending_registration
from app.services.registration import is_registered, start_registration, handle_message

webhook_bp = Blueprint("webhook", __name__)

# These come from your .env file -- never typed directly into code.
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

# This is a secret word WE make up ourselves -- not from Meta.
# We'll type this exact same word into Meta's dashboard shortly.
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "dekutcu_verify_2026")


@webhook_bp.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Handles Meta's ONE-TIME verification handshake.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verified successfully by Meta.")
        return challenge, 200
    else:
        print("Webhook verification FAILED -- token mismatch.")
        return "Verification failed", 403


@webhook_bp.route("/webhook", methods=["POST"])
def receive_message():
    """
    Handles every REAL incoming WhatsApp message from here on.
    """
    data = request.get_json()
    print("Incoming webhook data:", data)

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return jsonify({"status": "ignored, not a message"}), 200

        message = value["messages"][0]
        sender_number = message["from"]
        message_text = message["text"]["body"]

        print(f"Message from {sender_number}: {message_text}")

        reply_text = route_incoming_message(sender_number, message_text)

        send_whatsapp_message(
            to_number=sender_number,
            message_text=reply_text
        )

    except (KeyError, IndexError) as e:
        print(f"Could not parse incoming webhook: {e}")

    return jsonify({"status": "received"}), 200


def route_incoming_message(sender_number, message_text):
    """
    Stage 3: decides what to do with an incoming message.

    - Already-registered member: placeholder for now -- this is
      exactly what Stage 4 (the intent router) will take over.
    - Mid-registration (a pending row exists): hand off to the
      registration conversation to advance to the next step.
    - Brand-new sender (no member, no pending registration): kick
      off registration with the greeting + first question.
    """
    if is_registered(sender_number):
        # Stage 4 (intent router) will replace this.
        return "Hi again! (Full conversation handling is coming in a later stage.)"

    if get_pending_registration(sender_number) is not None:
        return handle_message(sender_number, message_text)

    return start_registration(sender_number)


def send_whatsapp_message(to_number, message_text):
    """
    Sends a WhatsApp message using Meta's API.
    This is the function every later stage (bandit messages, RAG
    answers, RSVP confirmations) will eventually call too.
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


@webhook_bp.route("/", methods=["GET"])
def health_check():
    """
    A simple endpoint just to confirm the server is alive at all.
    """
    return "DeKUTCU Engagement Bot is running.", 200
