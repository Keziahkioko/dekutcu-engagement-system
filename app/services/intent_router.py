"""
app/services/intent_router.py

Stage 4: The intent router.

This only runs for messages from ALREADY-REGISTERED members who are
NOT mid-registration -- app/routes/webhook.py handles that ordering
(unregistered -> registration flow; pending -> continue registration;
only otherwise does a message reach this module).

LLM-based intent classification (Groq) drives dispatch for everything
except the hardcoded STOP/RESUME keywords, which webhook.py checks
globally, before even the registered/pending checks -- an opt-out (or
opt back in) should never depend on registration status or on an AI
classifier having a good day.

Design note on scope: several intents below are stubbed -- they
correctly recognize what the member wants, but the real handler
(e.g. actually showing upcoming events) belongs to a later stage
that hasn't been built yet. update_details (AREA changes only --
see below), unsubscribe_followup, and withdraw_data_consent are
genuinely handled here since they only need the Members table, which
already exists. allocate_groups and reshuffle_groups (Stage 5) are
also genuinely handled -- they trigger the allocation engine in
app/services/allocation.py in a background thread (the ILP solve is
too slow to run inside the webhook request -- see Stage 5 planning
notes) and message the leader again once it's done.

Stage 6 adds group-LEADER management: nominate_group_leader,
view_group_leaders, resolve_pending_leader, and remove_group_leader
are genuinely handled, delegating the multi-step nomination
conversation to app/services/leader_assignment.py. A nominated
candidate's own accept/decline reply is handled right here, though,
via accept_leader_nomination -- it's a one-shot YES/NO, so it reuses
the existing pending_actions mechanism rather than needing its own
multi-step state (see leader_assignment.py's docstring for why that
split makes sense).

Also adds area-change reassignment: update_details only handles AREA
changes for real (name/year-of-study still fall through to a "not yet
supported" message -- they don't carry area's re-grouping
consequence). Delegates to app/services/area_change.py, which
deliberately does NOT auto-move an already-placed member into their
new area's groups -- it computes a recommendation and a leader has to
act via resolve_reassignments, same non-free-text numbered-list
pattern used throughout Stage 6.
"""

import os
import re
import json
import threading
from datetime import date

from app.models.member import (
    get_member_by_whatsapp_id,
    get_data_consenting_members,
    set_group_labels,
    count_group_placement_status,
    confirm_leader_nomination,
    decline_leader_nomination,
    get_leader_status,
)
from app.models.pending_action import set_pending_action, get_pending_action, clear_pending_action
from app.models.pending_fellowship_checkin import delete_pending_fellowship_checkin
from app.models.pending_reason_capture import delete_pending_reason_capture
from app.models.pending_feedback import delete_pending_feedback
from app.models.conversation_history import (
    get_recent_conversation,
    log_conversation_message,
    clear_conversation_history,
)
from app.database import get_connection
from app.services.allocation import allocate_members_topup, allocate_members_ilp
from app.services.whatsapp_client import send_whatsapp_message
from app.services.registration import AREAS
from app.services import leader_assignment
from app.services import area_change
from app.services import event_manager
from app.services import escalation
from app.services import fellowship_checkin
from app.services import feedback
from app.services import rag_companion
from app.services import org_contacts
from app.services.message_generator import display_name_for
from app.services.group_query import answer_group_question
from app.services.llm_client import create_chat_completion

STOP_KEYWORDS = {"stop", "unsubscribe"}
RESUME_KEYWORDS = {"resume"}

# Each intent's definition, shown to the LLM so it knows what each
# label means. Keep these short and behavior-focused.
INTENT_DEFINITIONS = {
    "greeting_smalltalk": "Casual greeting, small talk, thanks, or chit-chat with no specific request.",
    "general_question": "A general question about the organization, its beliefs, or its activities -- NOT about specific Bible study groups, group leaders, or group membership. Even a short follow-up like 'what about X' or 'and Y?' belongs to group_query instead if the conversation was just discussing groups/leaders/membership -- don't default here just because the message doesn't say the word 'group'.",
    "pastoral_question": "A personal or pastoral question about the member's OWN spiritual life, relationships, struggles or a decision they face, asking for guidance rather than information (e.g. 'I keep falling into the same sin, what should I do?', 'should I leave my church?') -- WITHOUT signs of real distress or crisis (that is needs_support).",
    "list_events": "Asking what events or activities are coming up -- a read-only question, NOT wanting to RSVP.",
    "event_rsvp": "Wanting to RSVP (yes/no/maybe) to a specific upcoming event -- NOT just asking what's coming up.",
    "create_event": "A leader wanting to create/announce a new event (Bible Study, cell group, fellowship, or a broadcast-only gathering like Sunday service).",
    "checkin_response": "Explaining or giving a reason for missing a session or event.",
    "feedback_response": "Giving feedback, a rating, or comments about a past event.",
    "purchase_study_guide": "Wanting to buy or pay for a Bible Study guide.",
    "update_details": "Wanting to change their own registered details (e.g. area, year of study, name).",
    "unsubscribe_followup": "Wanting to stop receiving follow-up/accountability check-ins specifically.",
    "resume_followup": "Wanting to start receiving follow-up/accountability check-ins again, having previously stopped them.",
    "withdraw_data_consent": "Wanting to withdraw consent entirely and stop being tracked/registered.",
    "request_human": "Explicitly asking to speak with, be contacted by, or reach a real person, a leader, or 'someone in charge' (including a 'group admin'), or asking for a leader's phone number.",
    "contact_info": "Asking for DeKUTCU's official contact details -- how to contact the CU, its office or secretary, a phone number or email for the CU. NOT asking for a specific leader's personal number.",
    "needs_support": "Message shows real distress or a serious personal struggle, even without explicitly asking for a human.",
    "leadership_query": "A leader asking for organizational data or a report (e.g. attendance numbers).",
    "send_announcement": "A leader wanting to broadcast a message to all members.",
    "allocate_groups": "A leader wanting to place new (ungrouped) members into Bible study groups.",
    "reshuffle_groups": "A leader wanting to fully regenerate every group from scratch, discarding existing placements.",
    "group_query": "A question about Bible study groups, group leaders, or group membership -- e.g. which group someone is in, who's in a group, how many groups exist, a summary of how allocation went, or who leads a group.",
    "nominate_group_leader": "A leader wanting to nominate or assign someone as a Bible study group leader for an area.",
    "view_group_leaders": "A leader wanting to see who the group leaders are -- confirmed, pending, or areas with no leader yet.",
    "resolve_pending_leader": "A leader wanting to manually confirm that a pending group-leader candidate has accepted, e.g. because they agreed in person rather than replying on WhatsApp.",
    "remove_group_leader": "A leader wanting to remove someone as a group leader.",
    "resolve_reassignments": "A leader wanting to review and act on pending member area-change reassignments.",
    "send_checkin": "A leader wanting to send out the 'were you at today's fellowship/service?' attendance check-in (and feedback question) to members right now, usually as a session is ending.",
    "unclear": "Doesn't confidently match any of the above.",
}

VALID_INTENTS = set(INTENT_DEFINITIONS.keys())

LEADER_ONLY_INTENTS = {
    "leadership_query", "send_announcement", "allocate_groups", "reshuffle_groups",
    "nominate_group_leader", "view_group_leaders", "resolve_pending_leader", "remove_group_leader",
    "resolve_reassignments", "create_event", "send_checkin",
}

# Guards against two allocation runs (each ~10-15 seconds) overlapping
# if a leader triggers this more than once before the first finishes.
_allocation_lock = threading.Lock()


def _build_system_prompt():
    lines = [
        "You classify an incoming WhatsApp message into exactly one intent label.",
        "Respond with ONLY a JSON object of the form {\"intent\": \"<label>\"}.",
        "Any earlier messages shown to you are recent conversation history, given "
        "only as CONTEXT to help you understand a short or ambiguous follow-up "
        "(e.g. a one-word reply, or 'then what am I') -- classify ONLY the final, "
        "most recent user message, never an earlier one.",
        "If the most recent assistant message was answering a group_query-type "
        "question (about groups, group leaders, or group membership) and the new "
        "message is a short or vague continuation of that same topic (e.g. 'what "
        "about X', 'and Y?', 'all of them', 'what of the members'), classify it as "
        "group_query too, even if it doesn't explicitly mention 'group' -- don't "
        "let it fall through to general_question or unclear. This applies EVEN "
        "WHEN X is something that could also be read as a standalone topic on its "
        "own (e.g. if the history was just discussing the Nyaribo Bible study "
        "group and the new message is 'what about internal hostels' or 'what "
        "about Gate A', that means the Bible study group(s) in that area too -- "
        "group_query -- NOT a general question about hostels or campus areas in "
        "general). The conversation history is the deciding factor, not whether "
        "the new message reads fine in isolation.",
        "Choose the single best-fitting label from this list:",
        "",
    ]
    for label, definition in INTENT_DEFINITIONS.items():
        lines.append(f"- {label}: {definition}")
    lines.append("")
    lines.append("If nothing fits confidently, use \"unclear\".")
    return "\n".join(lines)


_SYSTEM_PROMPT = _build_system_prompt()


def is_stop_message(text):
    """Hardcoded, non-LLM check for opting OUT of follow-up check-ins."""
    return text.strip().lower() in STOP_KEYWORDS


def is_resume_message(text):
    """Hardcoded, non-LLM check for opting back IN to follow-up check-ins."""
    return text.strip().lower() in RESUME_KEYWORDS


def classify_intent(message_text, whatsapp_id=None):
    """
    Calls Groq to classify the message into one of the intents in
    INTENT_DEFINITIONS.

    Falls back to "needs_support" (NOT "unclear") on any API-level
    error or unparseable response -- found via testing that Groq's
    model sometimes refuses to return the requested JSON label at all
    for messages describing acute self-harm/suicidal content, writing
    a full crisis-response paragraph instead, which fails json_object
    validation and raises here. Falling back to "unclear" in that
    exact case would silently drop the highest-stakes messages into a
    generic "didn't catch that" reply, defeating the entire point of
    Stage 11's escalation path. "needs_support" instead routes it into
    assess_severity, same "unnecessary check-in costs less than
    missing someone who needs help" reasoning already used there (see
    escalation.py) -- worst case, a false trigger asks an unneeded
    consent question; the alternative risks missing a real one.

    A genuinely unparseable-but-successful response (valid JSON, just
    an intent label outside VALID_INTENTS) is a different, milder case
    and still falls back to "unclear" below -- that only means the
    model picked a real answer that isn't one we recognize, not that
    classification broke entirely.

    If whatsapp_id is given, recent conversation history is prepended
    as context (see conversation_history.py) -- resolves short/
    ambiguous follow-ups that make no sense read in isolation. Purely
    additive context; classification still targets only message_text.
    """
    try:
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if whatsapp_id:
            for entry in get_recent_conversation(whatsapp_id):
                messages.append({"role": entry["role"], "content": entry["message_text"]})
        messages.append({"role": "user", "content": message_text})

        response = create_chat_completion(
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        intent = parsed.get("intent", "").strip()

        if intent in VALID_INTENTS:
            return intent
        return "unclear"

    except Exception as e:
        try:
            print(f"Intent classification failed, falling back to 'needs_support': {e}")
        except UnicodeEncodeError:
            print("Intent classification failed, falling back to 'needs_support' (error message omitted -- contained non-ASCII characters)")
        return "needs_support"


def set_followup_consent(whatsapp_id, value):
    """
    Sets ONLY followup_consent for a registered member -- shared by
    the global STOP/RESUME handlers and the unsubscribe_followup
    intent. Deliberately does NOT touch data_consent -- that's a
    separate, more serious action (withdraw_data_consent).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET followup_consent = %s WHERE whatsapp_id = %s",
        (value, whatsapp_id)
    )
    conn.commit()
    cursor.close()
    conn.close()


def opt_out_of_followup(whatsapp_id):
    """
    Everything opting out of follow-up check-ins means, in one place --
    shared by the global STOP keyword (webhook.py) and the confirmed
    unsubscribe_followup intent, so the two can never drift apart:
      - followup_consent off (data_consent untouched -- still a fully
        registered member, absences still recorded).
      - Any open check-in question closed (were-you-there, why-did-you-
        miss-it, feedback) -- otherwise their very next message would
        still be read as a reply to a check-in they just opted out of.
      - Their leader told ONCE, on the switch from opted-in to opted-out
        (so a repeat STOP doesn't re-notify): if the bot stops following
        up, a person should know, so nobody silently drops off. Same
        "which leader" rule as Stage 11 escalation. A leader opting out
        themselves is never notified about their own opt-out.
      - The member is told their leader was notified -- same
        transparency principle as Stage 11: the bot never contacts a
        leader about someone without saying so.
    Returns the reply text.
    """
    member = get_member_by_whatsapp_id(whatsapp_id)
    was_opted_in = bool(member and member["followup_consent"])

    set_followup_consent(whatsapp_id, False)
    delete_pending_fellowship_checkin(whatsapp_id)
    delete_pending_reason_capture(whatsapp_id)
    delete_pending_feedback(whatsapp_id)

    notified = []
    if was_opted_in:
        for leader_whatsapp_id, leader_name, _ in escalation.find_target_leaders(member):
            if leader_whatsapp_id == whatsapp_id:
                continue
            send_whatsapp_message(
                leader_whatsapp_id,
                f"{member['name']} has opted out of the bot's automatic check-ins. "
                "They're still a registered member -- you may want to stay in touch "
                "with them personally.",
            )
            notified.append(leader_name)

    reply = (
        "You won't receive follow-up check-ins anymore. You're still a fully "
        "registered member -- reply 'resume' anytime if you change your mind."
    )
    if notified:
        who = notified[0] if len(notified) == 1 else "your leaders"
        reply += f"\n\nI've let {who} know, so they can stay in touch with you personally."
    return reply


CONFIRMATION_QUESTIONS = {
    "unsubscribe_followup": (
        "Just to confirm: you'll stop receiving follow-up check-ins if you "
        "miss Bible study, fellowships, or events. You'll still be a fully "
        "registered member -- this only affects check-ins, nothing else. "
        "Your group leader will be let know, so they can stay in touch "
        "with you personally.\n\n"
        "Reply YES to confirm, or NO to cancel."
    ),
    "resume_followup": (
        "Just to confirm: you'll start receiving follow-up check-ins again "
        "if you miss something.\n\n"
        "Reply YES to confirm, or NO to cancel."
    ),
    "withdraw_data_consent": (
        "This is different from just pausing check-ins. Withdrawing consent "
        "fully removes you from tracking -- your Bible Study group "
        "placement, membership records, and anything tied to welfare "
        "eligibility. If you only want to stop check-ins, reply NO here "
        "and text STOP instead.\n\n"
        "Reply YES to confirm you want to withdraw completely, or NO to cancel."
    ),
    # allocate_groups isn't here -- its wording depends on how many
    # members are already placed (see _confirmation_text), so it's
    # computed dynamically rather than fixed like the others.
    "reshuffle_groups": (
        "This will move EVERYONE into new groups, not just new members -- "
        "existing group placements will NOT be preserved. This is a bigger "
        "action than the usual allocation command. It takes about 10-15 "
        "seconds -- I'll message you again once it's done.\n\n"
        "Reply YES to confirm you want a full reshuffle, or NO to cancel."
    ),
}


def _handle_unsubscribe_followup(member, text):
    set_pending_action(member["whatsapp_id"], "unsubscribe_followup")
    return CONFIRMATION_QUESTIONS["unsubscribe_followup"]


def _handle_resume_followup(member, text):
    set_pending_action(member["whatsapp_id"], "resume_followup")
    return CONFIRMATION_QUESTIONS["resume_followup"]


def _handle_withdraw_data_consent(member, text):
    set_pending_action(member["whatsapp_id"], "withdraw_data_consent")
    return CONFIRMATION_QUESTIONS["withdraw_data_consent"]


def withdraw_all_consent(whatsapp_id):
    """
    Sets BOTH consent flags to False -- the full, deliberate
    withdrawal, distinct from STOP/unsubscribe_followup which only
    ever touches followup_consent. Also clears group_label: a
    withdrawn member shouldn't keep "occupying" a slot the allocation
    engine thinks is taken, so a future run can offer it to someone else.
    Also purges conversation_history -- withdrawing consent should mean
    no trace of past exchanges is kept, not just the structured fields.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE members SET data_consent = FALSE, followup_consent = FALSE, group_label = NULL WHERE whatsapp_id = %s",
        (whatsapp_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
    clear_conversation_history(whatsapp_id)


# ---------------------------------------------------------------------
# Stage 5: allocate_groups / reshuffle_groups. Both run the actual
# allocation engine in a background thread (see module docstring for
# why) and message the leader again once it's done.
# ---------------------------------------------------------------------

def _handle_allocate_groups(member, text):
    placed, unplaced = count_group_placement_status()
    if unplaced == 0:
        return "Everyone's already been placed into a group -- there's nothing new to allocate right now."

    set_pending_action(member["whatsapp_id"], "allocate_groups")
    return _allocate_groups_confirmation_text(placed, unplaced)


def _allocate_groups_confirmation_text(placed, unplaced):
    """
    Worded differently for a genuine first-ever run (nobody placed
    yet, so "without moving anyone who's already placed" is confusing
    boilerplate) versus a routine top-up (some members already placed).
    """
    if placed == 0:
        return (
            f"This is your first time running allocation -- it'll place all "
            f"{unplaced} registered member(s) into their first Bible study "
            "groups. It takes about 10-15 seconds -- I'll message you again "
            "once it's done.\n\n"
            "Reply YES to confirm, or NO to cancel."
        )

    return (
        f"This will place {unplaced} new member(s) into Bible study groups, "
        f"without moving the {placed} member(s) already placed. It takes "
        "about 10-15 seconds -- I'll message you again once it's done.\n\n"
        "Reply YES to confirm, or NO to cancel."
    )


def _confirmation_text(action, whatsapp_id):
    """
    Returns the confirmation question for `action`. allocate_groups and
    accept_leader_nomination are computed fresh each time (the first
    depends on current placement counts, the second on which area
    THIS specific member is being asked about); every other action's
    text is static.
    """
    if action == "allocate_groups":
        placed, unplaced = count_group_placement_status()
        return _allocate_groups_confirmation_text(placed, unplaced)
    if action == "accept_leader_nomination":
        member = get_member_by_whatsapp_id(whatsapp_id)
        return (
            f"You've been asked to lead a Bible Study group in "
            f"{member['pending_leader_area']}. Reply YES to accept, or NO to decline."
        )
    if action == "send_checkin":
        activity_type = fellowship_checkin.todays_leader_checkin()
        if activity_type:
            return _send_checkin_confirmation_text(activity_type)
        return "Reply YES to send today's check-in, or NO to cancel."
    return CONFIRMATION_QUESTIONS[action]


def _handle_reshuffle_groups(member, text):
    set_pending_action(member["whatsapp_id"], "reshuffle_groups")
    return CONFIRMATION_QUESTIONS["reshuffle_groups"]


def _handle_group_query(member, text):
    """
    Open-ended, NOT leader-gated -- answers whatever's actually asked,
    scoped to what this specific member is allowed to see (own group
    for everyone, led group for group leaders, org-wide for exec
    leaders). Replaced the older fixed view_groups report entirely
    (its exact behavior is still reachable here via the list_groups
    tool) once that older intent started competing with this one for
    the same phrasings and losing detail in the process -- e.g. "list
    every group with its members" used to sometimes land on
    view_groups's counts-only summary instead of this, which could
    actually answer with real names. See app/services/group_query.py.
    """
    return answer_group_question(member, text)


def _handle_list_events(member, text):
    """
    A one-shot read, no pending state needed -- deliberately NOT
    LLM tool-calling like group_query: "what's coming up" is a single
    fixed shape, unlike group_query's genuinely varied phrasings, so a
    plain direct answer is enough.
    """
    return event_manager.list_upcoming_events()


def _handle_event_rsvp(member, text):
    return event_manager.start_rsvp(member["whatsapp_id"])


def _handle_create_event(member, text):
    return event_manager.start_create_event(member["whatsapp_id"])


# ---------------------------------------------------------------------
# Stage 6: group leader management. nominate/resolve/remove all
# delegate their multi-step conversation to leader_assignment.py --
# see that module's docstring. view_group_leaders is read-only, no
# multi-step conversation needed.
# ---------------------------------------------------------------------

def _handle_nominate_group_leader(member, text):
    return leader_assignment.start_nomination(member["whatsapp_id"])


def _handle_resolve_pending_leader(member, text):
    return leader_assignment.start_resolve_pending(member["whatsapp_id"])


def _handle_remove_group_leader(member, text):
    return leader_assignment.start_remove_leader(member["whatsapp_id"])


def _handle_resolve_reassignments(member, text):
    return area_change.start_resolve_reassignment(member["whatsapp_id"])


# Only area changes are genuinely handled for update_details right now
# -- name/year-of-study updates don't have area's re-grouping
# consequence and weren't part of what was scoped. Keyword-matched the
# same lightweight way registration.py matches a correction request.
_AREA_UPDATE_KEYWORDS = ("area", "hostel", "estate", "moved", "location", "residence")


def _handle_update_details(member, text):
    if any(keyword in text.lower() for keyword in _AREA_UPDATE_KEYWORDS):
        return area_change.start_area_change(member["whatsapp_id"])
    return (
        "Right now I can only help you update your area. If that's what you meant, "
        "try saying something like 'I want to update my area'. For other changes, "
        "please contact a leader directly."
    )


# ---------------------------------------------------------------------
# Feedback collection: leader-triggered check-in. The activity is
# inferred from today's date, never asked, so the wrong day's question
# can't be sent by mistake; the actual send runs in a background thread
# (messaging every member takes a while, same reason allocation is
# backgrounded) and the leader is messaged again once it's done. See
# fellowship_checkin.py for how this and the 9pm fallback never both
# go out on the same day.
# ---------------------------------------------------------------------

_TUESDAY = 1


def _send_checkin_confirmation_text(activity_type):
    return (
        f"This will ask every registered member whether they were at today's "
        f"{display_name_for(activity_type)}, and ask anyone who says YES for "
        "feedback.\n\nReply YES to send it now, or NO to cancel."
    )


def _handle_send_checkin(member, text):
    activity_type = fellowship_checkin.todays_leader_checkin()
    if activity_type is None:
        if date.today().weekday() == _TUESDAY:
            return (
                "Tuesday is Bible Study -- attendance there is marked by each group's "
                "leader instead (they get asked at 9pm), and attendees get the feedback "
                "question automatically after that."
            )
        return "There's no fellowship or service to check in for today."

    if fellowship_checkin.checkin_already_sent_today(activity_type):
        return (
            f"The check-in for today's {display_name_for(activity_type)} has already "
            "gone out -- members can already reply to it."
        )

    set_pending_action(member["whatsapp_id"], "send_checkin")
    return _send_checkin_confirmation_text(activity_type)


def _run_checkin_job(whatsapp_id):
    try:
        activity_type, sent = fellowship_checkin.send_leader_checkin()
        if activity_type is None:
            summary = "There's no fellowship or service to check in for today, so nothing was sent."
        elif sent is None:
            summary = f"The check-in for today's {display_name_for(activity_type)} had already gone out, so I didn't send it again."
        else:
            summary = f"Done -- sent the {display_name_for(activity_type)} check-in to {sent} member(s)."
    except Exception as e:
        summary = f"Something went wrong sending the check-in: {e}"
    send_whatsapp_message(whatsapp_id, summary)


def _handle_general_question(member, text):
    """Stage 12: answered from DeKUTCU's own materials, with citations -- see rag_companion.py."""
    return rag_companion.answer_question(member, text, pastoral=False)


def _handle_pastoral_question(member, text):
    """
    Stage 12: a personal question is ANSWERED from the materials (never
    with personal directives) AND always followed by a consent-first
    offer of a leader -- a deliberate, logged departure from the
    proposal's "route rather than answer" wording. See PROJECT_LOG.md.
    """
    return rag_companion.answer_question(member, text, pastoral=True)


def _handle_feedback_response(member, text):
    """
    Feedback nobody asked for -- recorded and safety-checked the same
    way as prompted feedback, but kept out of the response-rate figure.
    See feedback.record_unprompted_feedback.
    """
    return feedback.record_unprompted_feedback(member["whatsapp_id"], member["reg_number"], text)


def _handle_request_human(member, text):
    """
    Explicitly asking for a person already IS the consent -- escalates
    directly, no severity check and no consent question first (see
    escalation.py's module docstring for why this differs from
    needs_support).
    """
    reply = escalation.escalate_now(member, "request_human", text)
    if _ASKS_FOR_NUMBER.search(text):
        # Leaders' personal numbers are never shared with members (Keziah's
        # decision) -- the notified leader already has the member's number
        # and reaches out instead. DeKUTCU's official contacts are a
        # separate case: see org_contacts.py / the contact_info intent.
        reply += "\n\nFor privacy I don't share leaders' personal numbers -- they'll contact you directly."
    return reply


_ASKS_FOR_NUMBER = re.compile(r"\b(number|phone|contact)\b", re.IGNORECASE)


def _handle_needs_support(member, text):
    """
    Routes through the same two-tier severity model reason_capture.py
    uses for absence-reply distress -- acute_risk escalates regardless
    of consent (always transparently); distress asks first; none means
    the classifier didn't actually find distress in THIS message even
    though the intent router's own (coarser) classification flagged it,
    so it gets a plain supportive reply instead of a false escalation.
    """
    severity = escalation.assess_severity(text)

    if severity == "acute_risk":
        return escalation.escalate_acute(member, "needs_support", text)

    if severity == "distress":
        return escalation.start_consent_flow(member["whatsapp_id"], "needs_support", text)

    return (
        "I hear you -- thanks for sharing that with me. If things ever feel "
        "like too much, please don't hesitate to reach out to a leader directly."
    )


def _handle_view_group_leaders(member, text):
    confirmed, pending = get_leader_status()
    covered_areas = {area for area, _, _ in confirmed} | {area for area, _ in pending}
    unassigned_areas = [a for a in AREAS if a not in covered_areas]

    lines = ["Group leaders:"]
    if confirmed:
        lines.append("")
        lines.append("Confirmed:")
        # leads_group_label (e.g. "Bomas #2") if matched to a specific
        # formed group yet, else falls back to just the area name.
        lines.extend(f"- {group_label or area}: {name}" for area, name, group_label in confirmed)
    if pending:
        lines.append("")
        lines.append("Pending (awaiting their response):")
        lines.extend(f"- {area}: {name}" for area, name in pending)
    if unassigned_areas:
        lines.append("")
        lines.append("No leader yet: " + ", ".join(unassigned_areas))

    return "\n".join(lines)


def _resolve_leader_nomination(whatsapp_id, accepted):
    """
    Handles the CANDIDATE's own reply to a leader nomination -- notifies
    whoever nominated them either way, so they're not left wondering.
    """
    member = get_member_by_whatsapp_id(whatsapp_id)
    area = member["pending_leader_area"]
    nominator_whatsapp_id = member["pending_leader_nominator"]

    if accepted:
        confirm_leader_nomination(whatsapp_id)
        send_whatsapp_message(nominator_whatsapp_id, f"{member['name']} accepted -- they're now the leader for {area}.")
        leader_assignment.match_and_notify(area)
        return f"Great, you're now the leader for {area}! Thank you."

    decline_leader_nomination(whatsapp_id)
    send_whatsapp_message(nominator_whatsapp_id, f"{member['name']} declined the {area} leader role.")
    return "No problem -- thanks for letting us know."


def _start_allocation_job(whatsapp_id, action):
    """
    Tries to claim _allocation_lock and, if successful, starts the
    background job. If a run is already in progress, tells the leader
    to wait instead of starting a second, overlapping one.
    """
    if not _allocation_lock.acquire(blocking=False):
        return "An allocation is already in progress -- please wait for it to finish."

    mode = "reshuffle" if action == "reshuffle_groups" else "topup"
    thread = threading.Thread(target=_run_allocation_job, args=(whatsapp_id, mode), daemon=True)
    thread.start()

    return "Working on it -- I'll message you again once it's done (about 10-15 seconds)."


def _run_allocation_job(whatsapp_id, mode):
    """
    Runs in the background thread started by _start_allocation_job.
    Always releases _allocation_lock when done, even on error, so a
    failure can't leave the system permanently "stuck busy".
    """
    try:
        members = get_data_consenting_members()

        if mode == "reshuffle":
            result = allocate_members_ilp(members)
            updates = _labels_from_groups(result["groups"])
        else:
            result = allocate_members_topup(members)
            updates = result["updates"]

        if updates:
            set_group_labels(updates)
            for area in {_area_from_label(label) for label in updates.values()}:
                leader_assignment.match_and_notify(area)

        summary = _build_allocation_summary(updates, result["flagged_areas"], mode)
    except Exception as e:
        summary = f"Something went wrong while generating groups: {e}"
    finally:
        _allocation_lock.release()

    send_whatsapp_message(whatsapp_id, summary)


def _area_from_label(group_label):
    """"Bomas #2" -> "Bomas" -- group labels are always "{area} #{n}"."""
    return group_label.rsplit(" #", 1)[0]


def _labels_from_groups(groups_by_area):
    """Converts allocate_members_ilp's {area: [[member,...],...]} into {reg_number: label}."""
    updates = {}
    for area, groups in groups_by_area.items():
        for i, group_members in enumerate(groups, start=1):
            label = f"{area} #{i}"
            for member in group_members:
                updates[member["reg_number"]] = label
    return updates


def _build_allocation_summary(updates, flagged_areas, mode):
    verb = "Reshuffle" if mode == "reshuffle" else "Allocation"

    if not updates and not flagged_areas:
        return f"{verb} complete -- no new members needed placing."

    group_count = len(set(updates.values()))
    lines = [f"{verb} complete. {len(updates)} member(s) placed into {group_count} group(s)."]

    if flagged_areas:
        lines.append("")
        lines.append(f"{len(flagged_areas)} area(s) flagged for manual placement (too few members):")
        for area, area_members in flagged_areas.items():
            lines.append(f"- {area}: {len(area_members)}")

    return "\n".join(lines)


def handle_pending_action_response(whatsapp_id, text):
    """
    Called by webhook.py when a member has a pending action awaiting
    YES/NO confirmation. Executes or cancels it accordingly. Returns
    the reply text.
    """
    pending = get_pending_action(whatsapp_id)
    if pending is None:
        return None  # shouldn't happen -- webhook.py only calls this when one exists

    action = pending["action"]
    answer = text.strip().lower()

    if answer not in ("yes", "no"):
        # Re-ask rather than silently falling through to the intent
        # router -- a half-confirmed serious action shouldn't be lost
        # to an ambiguous reply.
        return f"Please reply YES or NO.\n\n{_confirmation_text(action, whatsapp_id)}"

    clear_pending_action(whatsapp_id)

    if action == "accept_leader_nomination":
        # Own branch, not the generic "no" shortcut below -- declining
        # needs to clear the pending fields AND notify whoever sent
        # the nomination, not just drop a pending_actions row.
        return _resolve_leader_nomination(whatsapp_id, accepted=(answer == "yes"))

    if answer == "no":
        return "No problem, nothing has changed."

    if action == "unsubscribe_followup":
        return opt_out_of_followup(whatsapp_id)
    elif action == "resume_followup":
        set_followup_consent(whatsapp_id, True)
        return "Great, follow-up check-ins are back on."
    elif action == "withdraw_data_consent":
        withdraw_all_consent(whatsapp_id)
        return (
            "Your consent has been withdrawn and you're no longer an "
            "active tracked member. If you'd like to fully rejoin later, "
            "just message me again to re-register."
        )
    elif action in ("allocate_groups", "reshuffle_groups"):
        return _start_allocation_job(whatsapp_id, action)
    elif action == "send_checkin":
        threading.Thread(target=_run_checkin_job, args=(whatsapp_id,), daemon=True).start()
        return "Sending it now -- I'll message you once it's gone out."

    return "Something went wrong processing that -- please try again."


def handle_message(whatsapp_id, message_text):
    """
    Main entry point for a message from a confirmed, fully-registered
    member (not mid-registration). Returns the reply text to send.

    Note: STOP/RESUME are checked globally in webhook.py, before even
    the registered/pending-registration checks -- by the time a
    message reaches this function, it's already known not to be one.

    Logs this exchange to conversation_history AFTER handling it, not
    before -- so classify_intent/group_query's own history fetch for
    THIS call never includes the message currently being processed.
    Deliberately scoped to just this general chat path -- registration,
    pending-action confirmations, leader nomination, and area-change
    flows have their own dedicated step-tracking and don't need this.
    """
    member = get_member_by_whatsapp_id(whatsapp_id)
    intent = classify_intent(message_text, whatsapp_id)

    if intent in LEADER_ONLY_INTENTS and not member["is_leader"]:
        # Don't confirm the feature exists to a non-leader -- just
        # fall back to the generic "can't help with that" response.
        intent = "unclear"

    handler = _STUB_HANDLERS.get(intent, _handle_unclear)
    reply = handler(member, message_text)

    log_conversation_message(whatsapp_id, "user", message_text)
    log_conversation_message(whatsapp_id, "assistant", reply)

    return reply


# ---------------------------------------------------------------------
# Handlers. update_details, unsubscribe_followup, and
# withdraw_data_consent will be replaced with real multi-step logic
# in the next build step -- stubbed here for now so the full router
# can be tested end-to-end first.
# ---------------------------------------------------------------------

def _handle_unclear(member, text):
    return "Sorry, I didn't quite catch that. Could you rephrase, or let me know what you're looking for?"


def _handle_stub(feature_name):
    def handler(member, text):
        return f"({feature_name} is coming in a later stage -- thanks for your patience!)"
    return handler


_STUB_HANDLERS = {
    "greeting_smalltalk": lambda member, text: f"Hey {member['name'].split()[0]}! How can I help?",
    "general_question": _handle_general_question,
    "pastoral_question": _handle_pastoral_question,
    "list_events": _handle_list_events,
    "event_rsvp": _handle_event_rsvp,
    "create_event": _handle_create_event,
    "checkin_response": _handle_stub("Check-in handling"),
    "feedback_response": _handle_feedback_response,
    "purchase_study_guide": _handle_stub("Study guide payments"),
    "update_details": _handle_update_details,
    "unsubscribe_followup": _handle_unsubscribe_followup,
    "resume_followup": _handle_resume_followup,
    "withdraw_data_consent": _handle_withdraw_data_consent,
    "request_human": _handle_request_human,
    "contact_info": lambda member, text: org_contacts.contact_reply(),
    "needs_support": _handle_needs_support,
    "leadership_query": _handle_stub("Leadership reports"),
    "send_announcement": _handle_stub("Sending announcements"),
    "allocate_groups": _handle_allocate_groups,
    "reshuffle_groups": _handle_reshuffle_groups,
    "group_query": _handle_group_query,
    "nominate_group_leader": _handle_nominate_group_leader,
    "view_group_leaders": _handle_view_group_leaders,
    "resolve_pending_leader": _handle_resolve_pending_leader,
    "remove_group_leader": _handle_remove_group_leader,
    "resolve_reassignments": _handle_resolve_reassignments,
    "send_checkin": _handle_send_checkin,
    "unclear": _handle_unclear,
}
