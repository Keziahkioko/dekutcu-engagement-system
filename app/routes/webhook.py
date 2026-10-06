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

import hashlib
import hmac
import os
import time
import threading
from flask import Blueprint, request, jsonify

from app.models.pending_registration import get_pending_registration, delete_pending_registration
from app.models.pending_action import get_pending_action
from app.models.pending_leader_nomination import get_pending_leader_nomination, delete_pending_leader_nomination
from app.models.pending_area_change import get_pending_area_change, delete_pending_area_change
from app.models.pending_reassignment_resolution import get_pending_reassignment_resolution, delete_pending_reassignment_resolution
from app.models.pending_event_creation import get_pending_event_creation, delete_pending_event_creation
from app.models.pending_rsvp import get_pending_rsvp, delete_pending_rsvp
from app.models.pending_attendance_marking import get_pending_attendance_marking
from app.models.pending_fellowship_checkin import get_pending_fellowship_checkin, mark_checkin_flag
from app.models.pending_reason_capture import get_pending_reason_capture
from app.models.pending_escalation_consent import get_pending_escalation_consent
from app.models.pending_feedback import get_pending_feedback
from app.models.pending_exec_role import get_pending_exec_role, delete_pending_exec_role
from app.models.member_question import get_pending_question_ask
from app.models.pending_message import (
    enqueue_message,
    claim_next_message,
    delete_pending_message,
    claim_message_id,
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
from app.services import exec_roles
from app.services import member_questions
from app.services import study_guides
from app.services import conversation
from app.services import announcements
from app.services import number_change
from app.services import guide_coordinator
from app.services import guide_batches
from app.services import guide_handover
from app.models.study_guide import get_pending_guide_creation, get_pending_guide_purchase, delete_pending_guide_creation
from app.models.last_flow_reply import get_last_flow_reply, set_last_flow_reply, clear_last_flow_reply, fingerprint
from app.database import get_connection
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
            send_whatsapp_message(to_number=message["sender_number"],
                                  message_text=reply_text or "Sorry, I didn't quite catch that -- could you say it another way?")
        except Exception as e:
            print(f"Background message processing failed for {message['sender_number']}: {e}")
            # QA 2026-10-01: a failure used to leave the member with NO reply at all.
            # Never include the error itself -- nothing internal reaches a member.
            try:
                send_whatsapp_message(to_number=message["sender_number"],
                                      message_text="Sorry, something went wrong on my end -- please try again in a moment.")
            except Exception as send_error:
                print(f"Couldn't send the apology either: {send_error}")
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
    if not _signature_ok(request):
        print("Webhook REFUSED: missing or invalid X-Hub-Signature-256 (not from Meta).")
        return jsonify({"status": "forbidden"}), 403

    data = request.get_json(silent=True) or {}

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for message in change.get("value", {}).get("messages", []):
                    _accept_message(message)
                # Delivery reports: a message WhatsApp couldn't deliver tells the number-change
                # check that the old number can't answer (e.g. they used WhatsApp's "Change number").
                for status in change.get("value", {}).get("statuses", []):
                    if status.get("status") == "failed":
                        number_change.handle_delivery_failure(status.get("id"))
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        print(f"Could not parse incoming webhook: {e}")

    return jsonify({"status": "received"}), 200


# Message types members can send that the bot can't read. Reactions and system notices are ignored silently.
_SILENT_TYPES = {"reaction", "system", "unsupported", "ephemeral", "request_welcome"}
_NOT_TEXT_REPLY = ("I can only read typed text messages for now -- please type what you need and I'll help 🙂")


def _accept_message(message):
    """
    One incoming message. QA 2026-10-01 fixes:
      - Meta re-sends a webhook it thinks failed: each message ID is accepted once only;
      - images, stickers, voice notes, locations... used to get NO reply at all -- now a short
        note that only text is understood (sent at once, nothing queued);
      - every message in a delivery is handled, not just the first.
    """
    sender_number = message["from"]
    message_id = message.get("id")
    if message_id and not claim_message_id(message_id):
        print(f"Ignoring a repeat delivery of message {message_id}")
        return
    kind = message.get("type", "text")
    if kind != "text" or "text" not in message:
        if kind not in _SILENT_TYPES:
            send_whatsapp_message(to_number=sender_number, message_text=_NOT_TEXT_REPLY)
        return
    message_text = message["text"]["body"]
    backlog_count = enqueue_message(sender_number, message_text)
    if backlog_count >= _BACKLOG_FILLER_THRESHOLD:
        send_whatsapp_message(to_number=sender_number, message_text=_FILLER_REPLY)


_warned_no_app_secret = []


def _signature_ok(req):
    """
    Proves a webhook POST really came from Meta (QA 2026-10-01): Meta signs every delivery with
    the app secret -- header X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body>. Without
    this check, anyone who learned the webhook address could post messages "from" any number,
    including a leader's, and the bot would act on them.

    META_APP_SECRET (Meta app dashboard -> App settings -> Basic -> App secret) must be set in
    .env and on Render. Until it is, requests are still accepted -- so adding this can't take
    the live bot down -- with a loud warning in the logs.
    """
    secret = os.getenv("META_APP_SECRET", "").strip()
    if not secret:
        if not _warned_no_app_secret:
            print("WARNING: META_APP_SECRET is not set -- webhook signatures are NOT being checked.")
            _warned_no_app_secret.append(True)
        return True
    header = req.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), req.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


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
        # A leader answering a member's relayed question ("ANSWER 12 ...") --
        # same early placement and fall-through as CLAIM.
        if member_questions.is_answer_message(message_text):
            reply = member_questions.handle_answer(sender_number, message_text)
            if reply is not None:
                return reply
        # A leader approving/denying a member's move to a new number ("APPROVE 4").
        if number_change.is_decision_message(message_text):
            reply = number_change.handle_decision(sender_number, message_text)
            if reply is not None:
                return reply
        # A group leader confirming a batch of guides ("RECEIVED 10") -- any time.
        if guide_batches.is_received_message(message_text):
            reply = guide_batches.handle_received(sender_number, message_text)
            if reply is not None:
                return reply
        # The member was asked for their NEW number ("I'm changing my number").
        if number_change.get_asking(sender_number) is not None:
            reply = number_change.handle_new_number_reply(sender_number, message_text)
            if reply is not None:
                return reply
        # A way out of anything the member STARTED (QA 2026-10-01: flows used to
        # trap people until they typed exactly "cancel").
        if conversation.is_cancel(message_text):
            cleared = _cancel_started_flows(sender_number)
            if cleared:
                return _cancelled_reply(cleared)
        # A question or a sentence at a step expecting a number, date or choice is
        # a NEW message: drop that flow and handle the message normally.
        note = _escape_structured_step(sender_number, message_text)
        if note:
            return route_incoming_message(sender_number, message_text) + note
        if get_pending_action(sender_number) is not None:
            reply = handle_pending_action_response(sender_number, message_text)
            if reply is not None:
                return reply
            # Not a yes/no: the confirmation was dropped WITHOUT acting (a leader
            # nomination invitation is kept), and the message is handled normally.
            dropped = get_pending_action(sender_number) is None
            return _route_after_confirmation(sender_number, message_text) + (_NOTHING_CHANGED if dropped else "")
        return _route_after_confirmation(sender_number, message_text)

    # A member who told us in advance they're moving to THIS number: finish the move.
    reply = number_change.on_unregistered_message(sender_number)
    if reply is not None:
        return reply

    if get_pending_registration(sender_number) is not None:
        return handle_message(sender_number, message_text)

    return start_registration(sender_number)


# "Never loop forever" (Keziah, 2026-10-06): the flows a member can be part-way through, in the
# order _route_flows checks them -- (table, what it is, how to restart it, how to stop it).
_LOOP_FLOWS = [
    ("pending_leader_nominations", "the group-leader change", "nominate a group leader", delete_pending_leader_nomination),
    ("pending_area_changes", "the area change", "change my area", delete_pending_area_change),
    ("pending_reassignment_resolutions", "the reassignments", "resolve reassignments", delete_pending_reassignment_resolution),
    ("pending_exec_role", "recording exec roles", "update the exec roles", delete_pending_exec_role),
    ("pending_event_creation", "creating the event", "create an event", delete_pending_event_creation),
    ("pending_handover", "recording the hand-over", "hand over guides", guide_handover.delete_pending),
    ("pending_batch", "recording the batch", "give guides to a leader", guide_batches.delete_pending),
    ("pending_coordinator_choice", "choosing the Guides Coordinator", "appoint the guides coordinator",
     guide_coordinator.delete_pending),
    ("pending_announcement", "the announcement", "send an announcement", announcements.delete_pending_announcement),
    ("pending_guide_creation", "starting the new study guide", "start a new study guide", delete_pending_guide_creation),
    ("pending_rsvps", "the RSVP", "RSVP", delete_pending_rsvp),
]


def _open_flow(sender_number):
    """The first flow (from _LOOP_FLOWS) the member is part-way through, or None -- one database call."""
    sql = " UNION ALL ".join(f"SELECT {i} AS i FROM {t} WHERE whatsapp_id = %s"
                             for i, (t, *_rest) in enumerate(_LOOP_FLOWS))
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT MIN(i) AS i FROM ({sql}) open_flows", [sender_number] * len(_LOOP_FLOWS))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return None if row is None or row["i"] is None else _LOOP_FLOWS[row["i"]]


def _route_after_confirmation(sender_number, message_text):
    """
    _route_flows, plus "never loop forever": if the bot is about to send, in the same flow, EXACTLY the
    reply it sent last time, the member has given two unclear answers in a row -- stop the flow instead
    (re-asks like "Please reply 1 or 2" are word-for-word the same each time). Only a fingerprint of the
    last reply is kept. A new attempt always starts with no flow open, which overwrites the old fingerprint.
    """
    before = _open_flow(sender_number)
    reply = _route_flows(sender_number, message_text)
    after = _open_flow(sender_number)
    if after is None:
        return reply
    table, what, restart, stop = after
    last = get_last_flow_reply(sender_number)
    if after is before and last and last["flow"] == table and last["reply_hash"] == fingerprint(reply):
        stop(sender_number)
        clear_last_flow_reply(sender_number)
        return (f"Sorry, I'm not following -- I've stopped {what} so you're not stuck. "
                f"Say \"{restart}\" whenever you want to start again.")
    set_last_flow_reply(sender_number, table, reply)
    return reply


def _route_flows(sender_number, message_text):
    """A registered member's routing after the YES/NO confirmation step."""
    if get_pending_leader_nomination(sender_number) is not None:
        return leader_assignment.handle_message(sender_number, message_text)
    if get_pending_area_change(sender_number) is not None:
        return area_change.handle_message(sender_number, message_text)
    if get_pending_reassignment_resolution(sender_number) is not None:
        return area_change.handle_resolution_message(sender_number, message_text)
    if get_pending_exec_role(sender_number) is not None:
        return exec_roles.handle_message(sender_number, message_text)
    if get_pending_event_creation(sender_number) is not None:
        return event_manager.handle_create_event_message(sender_number, message_text)
    if guide_handover.get_pending(sender_number) is not None:
        # A leader picking who they gave guides to -- anything else is handled normally.
        reply = guide_handover.handle_message(sender_number, message_text)
        if reply is not None:
            return reply
    if guide_batches.get_pending(sender_number) is not None:
        # The Guides Coordinator recording a batch for a group leader -- expires after 30 minutes.
        reply = guide_batches.handle_message(sender_number, message_text)
        if reply is not None:
            return reply
    if guide_coordinator.get_pending(sender_number) is not None:
        # The Director appointing / changing the Guides Coordinator -- expires after 30 minutes.
        reply = guide_coordinator.handle_message(sender_number, message_text)
        if reply is not None:
            return reply
    if announcements.get_pending_announcement(sender_number) is not None:
        # A leader part-way through an announcement -- expires after 30 minutes.
        reply = announcements.handle_message(sender_number, message_text)
        if reply is not None:
            return reply
    if get_pending_guide_creation(sender_number) is not None:
        # An exec leader starting a new study guide -- expires after 30
        # minutes, then the message is routed normally (study_guides.py).
        reply = study_guides.handle_start_guide_message(sender_number, message_text)
        if reply is not None:
            return reply
    if get_pending_guide_purchase(sender_number) is not None:
        # "Which number should I send the M-Pesa prompt to?" -- anything that isn't
        # an answer drops the question and is routed normally (study_guides.py).
        reply = study_guides.handle_purchase_reply(sender_number, message_text)
        if reply is not None:
            return reply
    if get_pending_rsvp(sender_number) is not None:
        return event_manager.handle_rsvp_message(sender_number, message_text)
    if get_pending_attendance_marking(sender_number) is not None:
        # A leader's "who was absent?" question stays open until answered;
        # a message that isn't an answer is routed normally, with ONE gentle
        # reminder the first time (fixed 2026-09-30 -- it used to trap them).
        reply = attendance.handle_attendance_marking_message(sender_number, message_text)
        if reply is not None:
            return reply
        return _route_remaining(sender_number, message_text) + attendance.take_reminder(sender_number)
    return _route_remaining(sender_number, message_text)


_NOTHING_CHANGED = "\n\n(I didn't go ahead with that earlier request -- just ask again if you still want it.)"

# Flows a member STARTS themselves, cleared by "cancel" / "never mind" / "start over"...
# (Bot-initiated questions -- attendance marking, check-ins, reasons, feedback, consent --
# have their own fall-through rules.)
_STARTED_FLOW_TABLES = [
    "pending_leader_nominations", "pending_area_changes", "pending_reassignment_resolutions", "pending_exec_role",
    "pending_event_creation", "pending_rsvps", "pending_guide_creation", "pending_guide_purchase",
    "pending_announcement", "pending_coordinator_choice", "pending_batch", "pending_handover",
]


def _cancel_started_flows(sender_number):
    """
    Clears every member-started flow (and any YES/NO confirmation except a leader-nomination
    invitation). Returns the names of the tables that had something open (empty if nothing was).
    """
    from app.database import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cleared = []
    for table in _STARTED_FLOW_TABLES:
        cursor.execute(f"DELETE FROM {table} WHERE whatsapp_id = %s", (sender_number,))
        if cursor.rowcount:
            cleared.append(table)
    cursor.execute("""DELETE FROM pending_actions WHERE whatsapp_id = %s
                      AND action NOT IN ('accept_leader_nomination', 'confirm_number_change')""", (sender_number,))
    if cursor.rowcount:
        cleared.append("pending_actions")
    conn.commit()
    cursor.close()
    conn.close()
    return cleared


def _cancelled_reply(cleared):
    """Says plainly what didn't happen where it matters -- above all, that no payment was started."""
    if "pending_guide_purchase" in cleared:
        return "Okay -- no payment was started. Ask me anytime if you'd like to buy the guide."
    if "pending_guide_creation" in cleared:
        return "Okay -- no new study guide was started."
    if "pending_actions" in cleared:
        return "Okay, I've stopped that -- nothing has changed. What would you like to do next?"
    return "Okay, I've stopped that. What would you like to do next?"


# (getter, steps that expect a number/date/choice -- None means every step, deleter, what to call it, how to restart)
def _structured_flows():
    return [
        (get_pending_rsvp, None, delete_pending_rsvp, "the RSVP", "RSVP"),
        (get_pending_event_creation, {"awaiting_type", "awaiting_date", "awaiting_time", "awaiting_confirm"},
         delete_pending_event_creation, "creating the event", "create an event"),
        (get_pending_leader_nomination, {"awaiting_area", "awaiting_bypass_choice", "awaiting_candidate_source_area",
                                         "removing_leader", "resolving_pending"},
         delete_pending_leader_nomination, "the group-leader change", "nominate a group leader"),
        (get_pending_exec_role, {"awaiting_area", "awaiting_office"}, delete_pending_exec_role,
         "recording exec roles", "update the exec roles"),
        (get_pending_area_change, None, delete_pending_area_change, "the area change", "change my area"),
        (announcements.get_pending_announcement, {"awaiting_kind", "awaiting_audience", "awaiting_confirm"},
         announcements.delete_pending_announcement, "the announcement", "send an announcement"),
        (guide_coordinator.get_pending, None, guide_coordinator.delete_pending,
         "choosing the Guides Coordinator", "appoint the guides coordinator"),
        (guide_batches.get_pending, None, guide_batches.delete_pending,
         "recording the batch", "give guides to a leader"),
        (get_pending_reassignment_resolution, None, delete_pending_reassignment_resolution,
         "the reassignments", "resolve reassignments"),
    ]


def _escape_structured_step(sender_number, message_text):
    """If the member is at a structured step and this reads as a new message, drop that flow; returns a short note (or '')."""
    if not conversation.looks_like_new_request(message_text):
        return ""
    for getter, steps, deleter, what, restart in _structured_flows():
        pending = getter(sender_number)
        if pending is None:
            continue
        if steps is not None and pending.get("step") not in steps:
            return ""      # a free-text step (a title, a name): sentences are normal answers there
        deleter(sender_number)
        return f"\n\n(I've stopped {what} -- say \"{restart}\" whenever you want to pick it up again.)"
    return ""


def _route_remaining(sender_number, message_text):
    """The rest of a registered member's routing, after the attendance-marking step."""
    if get_pending_fellowship_checkin(sender_number) is not None:
        # "Were you at X today?" -- a question or unrelated message isn't an answer (QA
        # 2026-10-01: it used to be recorded as an absence); then it's handled normally.
        reply = fellowship_checkin.handle_checkin_message(sender_number, message_text)
        if reply is not None:
            return reply
        # Talking about something else while the question is open: a bare YES/NO on a later
        # day is then checked first ("do you mean Wednesday's fellowship?") -- 2026-10-07.
        mark_checkin_flag(sender_number, "other_messages")
    if get_pending_reason_capture(sender_number) is not None:
        # Can hand the message back (expired, or not actually a reason) --
        # then it's routed normally, like feedback.
        reply = reason_capture.handle_reason_capture_message(sender_number, message_text)
        if reply is not None:
            return reply
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
    if get_pending_question_ask(sender_number) is not None:
        # "Reply ASK and I'll pass it to a leader" -- anything else drops
        # the offer and the message is routed normally.
        reply = member_questions.handle_ask_reply(sender_number, message_text)
        if reply is not None:
            return reply
    # "Did you receive your copy from Jane? YES/NO" -- non-blocking: a YES/NO answers it whenever
    # it comes; anything else is handled normally, with a one-time reminder line.
    reply = guide_handover.handle_receipt_answer(sender_number, message_text)
    if reply is not None:
        return reply
    return handle_intent_message(sender_number, message_text) + guide_handover.reminder_for(sender_number)


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