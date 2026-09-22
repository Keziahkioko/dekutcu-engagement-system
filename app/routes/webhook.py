"""
app/routes/webhook.py

STAGE 1 -- the skeleton webhook (a Blueprint, registered onto the
app inside app/__init__.py).

What this file does, in plain terms:
1. Meta needs to "verify" our webhook URL once, by sending a special
   GET request with a secret code. We must reply with that same code
   back, or Meta will refuse to ever send us real messages.
2. After that, every time someone messages our WhatsApp number, Meta
   sends a POST request to this same URL, containing what they said.
3. We read the message out of that POST request and route it.
"""

import os
import requests
from flask import Blueprint, request, jsonify

from app.models.pending_registration import get_pending_registration, delete_pending_registration
from app.models.pending_action import get_pending_action
from app.services.registration import is_registered, start_registration, handle_message
from app.services.intent_router import (
    is_stop_message,
    is_resume_message,
    set_followup_consent,
    handle_pending_action_response,
    handle_message as handle_intent_message,
)

webhook_bp = Blueprint("webhook", __name__)

# These come from your .env file -- never typed directly into code.
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

# This is a secret word WE make up ourselves -- not from Meta.
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
    Decides what to do with an incoming message, in strict order:

    1. STOP / RESUME keywords -- checked globally, before anything
       else, no matter what state the sender is in (registered,
       mid-registration, or brand new). These deliberately only
       affect followup_consent, not data_consent -- see Stage 4
       design notes. Neither depends on the LLM classifier.
    2. Already-registered member with a pending confirmation (e.g.
       they were just asked "are you sure?" for unsubscribe_followup,
       resume_followup, or withdraw_data_consent): their next reply
       is matched to that confirmation, NOT reclassified by the LLM.
    3. Already-registered member, no pending confirmation: hand off
       to the real intent router (Stage 4).
    4. Mid-registration (a pending row exists): continue the
       registration conversation.
    5. Brand-new sender: kick off registration.
    """
    if is_stop_message(message_text):
        return _handle_global_stop(sender_number)

    if is_resume_message(message_text):
        return _handle_global_resume(sender_number)

    if is_registered(sender_number):
        if get_pending_action(sender_number) is not None:
            return handle_pending_action_response(sender_number, message_text)
        return handle_intent_message(sender_number, message_text)

    if get_pending_registration(sender_number) is not None:
        return handle_message(sender_number, message_text)

    return start_registration(sender_number)


def _handle_global_stop(sender_number):
    """
    STOP means "stop sending me proactive check-ins" -- it only
    touches followup_consent. Full data withdrawal is a separate,
    deliberate action (withdraw_data_consent intent), not a keyword.
    """
    if is_registered(sender_number):
        set_followup_consent(sender_number, False)
        return (
            "You won't receive follow-up check-ins anymore. You're still "
            "a fully registered member -- reply 'resume' anytime if you "
            "change your mind."
        )

    if get_pending_registration(sender_number) is not None:
        delete_pending_registration(sender_number)
        return "Registration cancelled. Message me again anytime if you change your mind."

    return "No problem -- you're not currently registered, so there's nothing to stop. Message me anytime to get started."


def _handle_global_resume(sender_number):
    if is_registered(sender_number):
        set_followup_consent(sender_number, True)
        return "Welcome back! Follow-up check-ins are switched back on."

    return "You're not currently registered. Message me anytime to get started."


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


@webhook_bp.route("/", methods=["GET"])
def health_check():
    """
    A simple endpoint just to confirm the server is alive at all.
    """
    return "DeKUTCU Engagement Bot is running.", 200