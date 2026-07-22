"""
app/main.py

STAGE 1 -- the skeleton webhook.

What this file does, in plain terms:
1. Meta needs to "verify" our webhook URL once, by sending a special
   GET request with a secret code. We must reply with that same code
   back, or Meta will refuse to ever send us real messages.
2. After that, every time someone messages our WhatsApp number, Meta
   sends a POST request to this same URL, containing what they said.
3. We read the message out of that POST request, and (for now) just
   reply with a fixed test message, to prove the whole chain works.

Nothing intelligent happens yet -- no database, no bandit, no LLM.
That's deliberate. We want to prove the WIRING works first, so any
bug we hit later is a LOGIC bug, not a PLUMBING bug.
"""

import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load the .env file so we can read our secret keys without
# hardcoding them anywhere in this file.
load_dotenv()

app = Flask(__name__)

# These come from your .env file -- never typed directly into code.
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

# This is a secret word WE make up ourselves -- not from Meta.
# We'll type this exact same word into Meta's dashboard shortly.
# It's how Meta proves to us "yes, this verification request is
# really from Meta, not some random person guessing our URL."
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "dekutcu_verify_2026")


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Handles Meta's ONE-TIME verification handshake.

    When you paste your webhook URL into Meta's dashboard and click
    "Verify", Meta sends a GET request here with three pieces of
    info in the URL itself:
      - hub.mode          (will be the word "subscribe")
      - hub.verify_token  (should match OUR secret VERIFY_TOKEN)
      - hub.challenge     (a random number Meta wants echoed back)

    If the token matches, we prove we're the real owner of this URL
    by sending back exactly that challenge number. If it doesn't
    match, we refuse.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verified successfully by Meta.")
        # Must return the challenge as plain text, not JSON.
        return challenge, 200
    else:
        print("Webhook verification FAILED -- token mismatch.")
        return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    """
    Handles every REAL incoming WhatsApp message from here on.

    Meta sends a fairly deeply-nested JSON structure. We only care
    about a small part of it right now: who sent the message, and
    what they said.
    """
    data = request.get_json()
    print("Incoming webhook data:", data)  # helpful while we're learning

    try:
        # This is the exact path Meta buries the message inside.
        # We'll get comfortable with this shape -- it doesn't change.
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        # If there's no "messages" key, this webhook call was some
        # other kind of notification (e.g. a delivery receipt), not
        # an actual incoming message -- so we just acknowledge and
        # do nothing.
        if "messages" not in value:
            return jsonify({"status": "ignored, not a message"}), 200

        message = value["messages"][0]
        sender_number = message["from"]           # who sent it
        message_text = message["text"]["body"]     # what they said

        print(f"Message from {sender_number}: {message_text}")

        # For now: just reply with a fixed test message, to prove
        # the whole round trip works. No intelligence yet.
        send_whatsapp_message(
            to_number=sender_number,
            message_text="Hello! I received your message. (This is the Stage 1 test reply.)"
        )

    except (KeyError, IndexError) as e:
        # If the data doesn't have the shape we expected, log it
        # instead of crashing -- this happens sometimes with
        # non-message webhook events, and we don't want the whole
        # server to fall over because of it.
        print(f"Could not parse incoming webhook: {e}")

    # Meta expects a 200 OK response quickly, or it will assume
    # delivery failed and retry -- so we always return this at the end.
    return jsonify({"status": "received"}), 200


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


@app.route("/", methods=["GET"])
def health_check():
    """
    A simple endpoint just to confirm the server is alive at all --
    useful for checking deployment worked, totally separate from the
    WhatsApp webhook logic above.
    """
    return "DeKUTCU Engagement Bot is running.", 200


if __name__ == "__main__":
    app.run(port=5000, debug=True)