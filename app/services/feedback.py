"""
app/services/feedback.py

Feedback collection -- the proposal's WhatsApp-native replacement for
the Google Forms mechanism, evaluated on response rate against that
baseline (see app/models/feedback_request.py for how every request is
counted).

Only people who actually attended are asked, via three entry points
that plug into flows that already exist rather than a separate trigger:
  - Bible Study: after a group leader marks absentees (attendance.py),
    everyone NOT marked absent gets the question -- the same
    leader-confirmed attendance the bandit already relies on.
  - Fellowships (Mon/Wed/Thu/Fri): a YES to the "were you there?"
    check-in (fellowship_checkin.py) gets the question in the same
    reply, whether that check-in was leader-triggered live near the
    end of the session or sent by the 9pm fallback.
  - Sunday service: same as fellowships, but leader-triggered only and
    feedback-only -- see fellowship_checkin.py for why a NO there never
    becomes an absence.

One open question, free text -- feedback, questions, recommendations,
challenges, anything. No forced 1-5 rating: response rate is the
metric, and every extra step costs replies. Sorting replies into
feedback/question/recommendation/challenge is deliberately NOT done
live -- that's a batch job for Stage 13's reporting, which keeps each
live reply to the minimum number of LLM calls (a Sunday broadcast can
produce a burst of replies all at once against Groq's daily budget).

Two checks DO run live on every reply, and why:
  - is_feedback: after a large broadcast, hundreds of members have an
    open feedback question at once. Someone messaging "when is Bible
    Study?" that evening must NOT have that recorded as feedback -- if
    the reply isn't actually answering the question, the pending state
    is cleared and handle_feedback_message returns None, telling the
    webhook to route the message normally instead.
  - severity: a "challenge" can be real distress ("struggling to keep
    up spiritually, feeling alone"). Every real feedback reply goes
    through the SAME escalation.assess_severity used everywhere else
    (Stage 11) -- one safety classifier, not a second, subtly different
    one -- and escalates exactly the same way.

Open questions also expire after _EXPIRY_HOURS, much sooner than other
pending flows, for the same hijacking reason: an unanswered question
from last Sunday shouldn't swallow a message sent on Wednesday.
"""

import json
from datetime import datetime, timezone, timedelta, date

from app.models.feedback_request import (
    create_feedback_request,
    record_feedback_response,
    set_theme,
)
from app.services import rag_companion
from app.services import member_questions
from app.models.pending_feedback import (
    get_pending_feedback,
    start_pending_feedback,
    delete_pending_feedback,
)
from app.models.member import get_member_by_whatsapp_id
from app.services.llm_client import create_chat_completion
from app.services import escalation

_EXPIRY_HOURS = 12

_IS_FEEDBACK_SYSTEM_PROMPT = (
    "A church fellowship member was just asked by a WhatsApp bot: \"How was it? "
    "Share any feedback, questions, recommendations or challenges you have.\" "
    "Decide whether their reply is answering THAT question (any feedback, opinion, "
    "suggestion, question or challenge about the session, or about how they're "
    "doing) or is instead an unrelated message to the bot (e.g. asking when "
    "the next event is, asking about their group, a greeting unrelated to the "
    "session). Also decide whether the reply ASKS something the member wants answered "
    "about the session or the CU (e.g. 'why did we start so late today?', 'will the songs "
    "we sang be shared?', 'can I vote at the AGM?') -- not a rhetorical remark. Respond "
    "with ONLY a JSON object: "
    "{\"is_feedback\": true or false, \"is_question\": true or false}."
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def build_feedback_prompt(activity_display_name, name=None):
    greeting = f"Hey {name.split()[0]}, thanks" if name else "Thanks"
    return (
        f"{greeting} for being at {activity_display_name} today! How was it? "
        "Share any feedback, questions, recommendations or challenges you have -- "
        "we read every reply.\n\nReply 'skip' if you'd rather not."
    )


def begin_feedback(whatsapp_id, reg_number, activity_type, activity_date, trigger):
    """
    Records the request (so it counts toward response rate) and opens
    the pending question. Does NOT send anything -- callers either
    return build_feedback_prompt's text as their reply (same person) or
    send it directly first (a different person), then call this only
    once the send actually succeeded, so a failed send is never counted
    as a request that went unanswered.
    """
    request_id = create_feedback_request(reg_number, activity_type, activity_date, trigger, _now())
    start_pending_feedback(whatsapp_id, request_id, _now())
    return request_id


def _is_expired(pending):
    created_at = pending["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > timedelta(hours=_EXPIRY_HOURS)


def _classify_reply(text):
    """
    Returns (is_feedback, is_question) from ONE model call. is_question
    was added to the check that already existed, so spotting a question
    immediately costs nothing extra (Keziah wanted the "answered up to a
    day later" trade-off avoided -- see member_questions.py).
    """
    try:
        response = create_chat_completion(
            messages=[
                {"role": "system", "content": _IS_FEEDBACK_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content)
        return bool(parsed.get("is_feedback", True)), bool(parsed.get("is_question", False))
    except Exception as e:
        # Defaults to feedback -- recording an off-topic message as feedback
        # is a minor data blemish; the alternative (dropping it through
        # to normal routing) would also skip the severity check below.
        try:
            print(f"Feedback relevance check failed, treating as feedback: {e}")
        except UnicodeEncodeError:
            print("Feedback relevance check failed, treating as feedback (error message omitted -- contained non-ASCII characters)")
        return True, False


def handle_feedback_message(whatsapp_id, message_text):
    """
    Returns the reply text, OR None if this message isn't actually a
    reply to the feedback question (expired, or judged unrelated) --
    in which case the pending question has been cleared and the
    webhook should route the message normally instead.
    """
    pending = get_pending_feedback(whatsapp_id)
    if pending is None:
        return None

    if _is_expired(pending):
        delete_pending_feedback(whatsapp_id)
        return None

    text = message_text.strip()
    request_id = pending["feedback_request_id"]

    if text.lower() == "skip":
        delete_pending_feedback(whatsapp_id)
        record_feedback_response(request_id, None, None, _now())
        return "No problem -- thanks anyway!"

    is_feedback, is_question = _classify_reply(text)
    if not is_feedback:
        delete_pending_feedback(whatsapp_id)
        return None

    delete_pending_feedback(whatsapp_id)
    return _record_and_respond(whatsapp_id, request_id, text, is_question)


def record_unprompted_feedback(whatsapp_id, reg_number, text):
    """
    Feedback a member sends WITHOUT having been asked (the
    feedback_response intent) -- e.g. "last night's worship was
    amazing". Stored with trigger 'unprompted' and activity_type
    'general' (the bot can't know which session they mean) so leaders
    still see it, but deliberately EXCLUDED from response rate: nobody
    asked, so it isn't a response -- counting it would inflate the
    number compared against the Google Forms baseline. Goes through the
    exact same safety check as prompted feedback.
    """
    request_id = create_feedback_request(reg_number, "general", date.today(), "unprompted", None)
    return _record_and_respond(whatsapp_id, request_id, text.strip())


def _record_and_respond(whatsapp_id, request_id, text, is_question=False):
    """
    Shared by prompted and unprompted feedback: one safety check, one way
    of escalating. A QUESTION (with no distress) is answered from
    DeKUTCU's materials if they cover it -- with the member's way out,
    "reply ASK and I'll pass it to a leader" -- and otherwise relayed to
    the leaders anonymously (member_questions.py). Safety comes first:
    a question that also shows distress goes through escalation instead.
    """
    severity = escalation.assess_severity(text)
    record_feedback_response(request_id, text, severity, _now())

    if severity == "acute_risk":
        member = get_member_by_whatsapp_id(whatsapp_id)
        return escalation.escalate_acute(member, "feedback", text)

    if severity == "distress":
        return escalation.start_consent_flow(whatsapp_id, "feedback", text)

    if is_question:
        set_theme(request_id, "question")  # known now -- no need to wait for the 2pm sort
        member = get_member_by_whatsapp_id(whatsapp_id)
        answer = rag_companion.answer_from_materials(member, text)
        if answer:
            return (
                "Thanks for your feedback! Here's what I found on your question:\n\n"
                f"{answer}\n\n{member_questions.offer_ask(whatsapp_id, text, request_id)}"
            )
        return f"Thanks for your feedback! {member_questions.relay_to_leaders(member, text, request_id)}"

    return "Thanks for sharing -- it really helps the leadership team."
