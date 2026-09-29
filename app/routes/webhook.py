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
import time
import threading
from flask import Blueprint, request, jsonify

from app.models.pending_registration import get_pending_registration, delete_pending_registration
from app.models.pending_action import get_pending_action
from app.models.pending_leader_nomination import get_pending_leader_nomination
from app.models.pending_area_change import get_pending_area_change
from app.models.pending_reassignment_resolution import get_pending_reassignment_resolution
from app.models.pending_event_creation import get_pending_event_creation
from app.models.pending_rsvp import get_pending_rsvp
from app.models.pending_attendance_marking import get_pending_attendance_marking
from app.models.pending_fellowship_checkin import get_pending_fellowship_checkin
from app.models.pending_reason_capture import get_pending_reason_capture
from app.models.pending_escalation_consent import get_pending_escalation_consent
from app.models.pending_feedback import get_pending_feedback
from app.models.pending_message import (
    enqueue_message,
    claim_next_message,
    delete_pending_message,
)
from app.services.registration import is_registered, start_registration, handle_message
from app.services.intent_router import (
    is_stop_message,
    is_resume_message,
    set_followup_consent,
    opt_out_of_followup,
    handle_pending_action_response,
    handle_message as handle_intent_message,
)
from app.services import leader_assignment
from app.services import area_change
from app.services import event_manager
from app.services import attendance
from app.services import fellowship_checkin
from app.services import reason_capture
from app.services import escalation
from app.services import feedback
from app.services.whatsapp_client import send_whatsapp_message

webhook_bp = Blueprint("webhook", __name__)

# This is a secret word WE make up ourselves -- not from Meta.
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "dekutcu_verify_2026")

# Incoming messages are queued (durably, in the pending_messages
# table -- see app/models/pending_message.py) and handled by a small
# pool of background worker threads, instead of being processed inline
# during the webhook POST. Meta expects a prompt 200 OK on webhook
# delivery, or it assumes the delivery failed and resends the same
# payload -- reprocessing it a second time. Classifying intent (Groq)
# can occasionally take a few retried seconds (see llm_client.py), so
# answering Meta first and doing the real work after removes that
# timing pressure entirely.
#
# Kept deliberately modest rather than maximized: Groq's own
# per-minute limit only fits a handful of messages regardless of how
# many worker threads exist, so a bigger pool wouldn't add real
# throughput on its own (see PROJECT_LOG.md). Easy to raise later with
# real usage data -- just this one constant.
_NUM_WORKERS = 3

# If this many messages are already waiting when a new one arrives,
# send an instant, non-AI "got it" reply before the real one -- a
# WhatsApp user seeing nothing for a while reads very differently to
# a website visitor watching a loading spinner.
_BACKLOG_FILLER_THRESHOLD = 3
_FILLER_REPLY = "Got your message! I'm a little busy right now -- give me a moment and I'll get back to you."


def _process_queue_shard(shard_index):
    """
    Runs forever in one background thread, handling only messages
    whose sender hashes into this worker's shard -- so any one
    sender's own messages are always handled by the same worker, in
    order, never racing each other against shared pending-state rows,
    while different senders' messages can still process in parallel
    across the pool.

    The broad except here is deliberate and load-bearing: an unhandled
    exception killing this thread would silently stop it claiming any
    more of ITS shard's messages forever (though the other workers'
    shards would be unaffected) -- a single bad message could never
    do that under the old per-request model, where each request was
    independent.
    """
    while True:
        message = claim_next_message(shard_index, _NUM_WORKERS)
        if message is None:
            time.sleep(0.5)
            continue
        try:
            reply_text = route_incoming_message(message["sender_number"], message["message_text"])
            send_whatsapp_message(to_number=message["sender_number"], message_text=reply_text)
        except Exception as e:
            print(f"Background message processing failed for {message['sender_number']}: {e}")
        finally:
            delete_pending_message(message["id"])


def start_message_worker():
    for shard_index in range(_NUM_WORKERS):
        threading.Thread(target=_process_queue_shard, args=(shard_index,), daemon=True).start()


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

        backlog_count = enqueue_message(sender_number, message_text)
        if backlog_count >= _BACKLOG_FILLER_THRESHOLD:
            send_whatsapp_message(to_number=sender_number, message_text=_FILLER_REPLY)

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
       resume_followup, withdraw_data_consent, or a leader-nomination
       accept/decline): their next reply is matched to that
       confirmation, NOT reclassified by the LLM.
    3. Already-registered member mid-way through nominating a group
       leader (Stage 6, a multi-step conversation -- see
       leader_assignment.py), updating their own area, resolving a
       pending reassignment (see area_change.py), creating an event,
       RSVPing to one (see event_manager.py), marking Bible Study
       attendance as a leader, answering why they were absent (see
       attendance.py), answering a pending "is it okay to notify a
       leader?" consent question (see escalation.py), or answering a
       feedback question (see feedback.py -- the one flow that can hand
       the message back to step 4 if it isn't really feedback):
       continue that conversation.
    4. Already-registered member, no pending confirmation or
       in-progress conversation: hand off to the real intent router
       (Stage 4).
    5. Mid-registration (a pending row exists): continue the
       registration conversation.
    6. Brand-new sender: kick off registration.
    """
    if is_stop_message(message_text):
        return _handle_global_stop(sender_number)

    if is_resume_message(message_text):
        return _handle_global_resume(sender_number)

    if is_registered(sender_number):
        # A leader claiming an escalation case ("CLAIM 12") -- checked before
        # any pending flow, since they may be mid-way through something else.
        # Falls through if they have nothing to claim.
        if escalation.is_claim_message(message_text):
            reply = escalation.handle_claim(sender_number, message_text)
            if reply is not None:
                return reply
        if get_pending_action(sender_number) is not None:
            return handle_pending_action_response(sender_number, message_text)
        if get_pending_leader_nomination(sender_number) is not None:
            return leader_assignment.handle_message(sender_number, message_text)
        if get_pending_area_change(sender_number) is not None:
            return area_change.handle_message(sender_number, message_text)
        if get_pending_reassignment_resolution(sender_number) is not None:
            return area_change.handle_resolution_message(sender_number, message_text)
        if get_pending_event_creation(sender_number) is not None:
            return event_manager.handle_create_event_message(sender_number, message_text)
        if get_pending_rsvp(sender_number) is not None:
            return event_manager.handle_rsvp_message(sender_number, message_text)
        if get_pending_attendance_marking(sender_number) is not None:
            return attendance.handle_attendance_marking_message(sender_number, message_text)
        if get_pending_fellowship_checkin(sender_number) is not None:
            return fellowship_checkin.handle_checkin_message(sender_number, message_text)
        if get_pending_reason_capture(sender_number) is not None:
            return reason_capture.handle_reason_capture_message(sender_number, message_text)
        if get_pending_escalation_consent(sender_number) is not None:
            # A soft leader OFFER (Stage 12) returns None for anything but
            # YES/NO -- the offer is dropped and the message routed normally.
            reply = escalation.handle_consent_reply(sender_number, message_text)
            if reply is not None:
                return reply
        if get_pending_feedback(sender_number) is not None:
            # Unlike every other pending flow, this one can decline the
            # message (expired, or not actually feedback -- see
            # feedback.py) and let it fall through to normal routing.
            reply = feedback.handle_feedback_message(sender_number, message_text)
            if reply is not None:
                return reply
        return handle_intent_message(sender_number, message_text)

    if get_pending_registration(sender_number) is not None:
        return handle_message(sender_number, message_text)

    return start_registration(sender_number)


def _handle_global_stop(sender_number):
    """
    STOP means "stop sending me proactive check-ins" -- it only
    touches followup_consent (and closes any open check-in question,
    and tells their leader once -- see opt_out_of_followup). Full data
    withdrawal is a separate, deliberate action (withdraw_data_consent
    intent), not a keyword.
    """
    if is_registered(sender_number):
        return opt_out_of_followup(sender_number)

    if get_pending_registration(sender_number) is not None:
        delete_pending_registration(sender_number)
        return "Registration cancelled. Message me again anytime if you change your mind."

    return "No problem -- you're not currently registered, so there's nothing to stop. Message me anytime to get started."


def _handle_global_resume(sender_number):
    if is_registered(sender_number):
        set_followup_consent(sender_number, True)
        return "Welcome back! Follow-up check-ins are switched back on."

    return "You're not currently registered. Message me anytime to get started."


@webhook_bp.route("/", methods=["GET"])
def health_check():
    """
    A simple endpoint just to confirm the server is alive at all.
    """
    return "DeKUTCU Engagement Bot is running.", 200