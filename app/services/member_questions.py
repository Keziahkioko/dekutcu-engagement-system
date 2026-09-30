"""
app/services/member_questions.py

Stage 13: making sure a QUESTION asked in feedback gets an answer
(Keziah asked for this gap to be closed rather than logged, and for
both trade-offs to be avoided). feedback.py spots a question
immediately, in the relevance check it already makes -- no extra model
call -- and tries DeKUTCU's materials first (rag_companion). This module
handles the rest:
  - relay_to_leaders: a question the materials don't cover (or that the
    member sends on with ASK) goes to the exec leaders ANONYMOUSLY --
    feedback is anonymous, and leaders never learn who asked. Each can
    reply "ANSWER <n> <their answer>".
  - handle_answer: the FIRST answer is relayed to the member by the bot
    (with the leader's name); the other leaders are told it's answered.
  - offer_ask / handle_ask_reply: after an automatic answer, "reply ASK
    and I'll pass it to a leader" -- the member's way out if the answer
    wasn't enough. Never insistent (any other reply drops it) and it
    expires after 12 hours, like every other open question.
Unanswered questions show in the feedback report and the Sunday summary.
"""

import re
from datetime import datetime, timezone, timedelta

from app.models.member import get_all_leaders, get_member_by_whatsapp_id, get_member_by_reg_number
from app.models.member_question import (
    create_question, get_question, record_answer,
    get_pending_question_ask, start_pending_question_ask, delete_pending_question_ask,
)
from app.models.withdrawal import REMOVED_TEXT
from app.services.whatsapp_client import send_whatsapp_message

_ASK_EXPIRY_HOURS = 12
_ANSWER_PATTERN = re.compile(r"^\s*answer\s*#?\s*(\d+)\s*[:\-]?\s+(.+)$", re.IGNORECASE | re.DOTALL)

ASK_OFFER = "If that doesn't fully answer your question, reply ASK and I'll pass it to a leader."


def _now():
    return datetime.now(timezone.utc).isoformat()


def _leaders_except(reg_number):
    return [l for l in get_all_leaders() if l["whatsapp_id"] and l["reg_number"] != reg_number]


def relay_to_leaders(member, question, feedback_request_id=None):
    """Sends the question to every exec leader, anonymously. Returns the reply for the member."""
    question_id = create_question(member["reg_number"], question, feedback_request_id, _now())
    for leader in _leaders_except(member["reg_number"]):
        send_whatsapp_message(
            leader["whatsapp_id"],
            f"A member asked in their feedback: \"{question}\"\n\n"
            f"Reply ANSWER {question_id} followed by your answer, and I'll pass it on to them "
            "(they stay anonymous). The first answer is the one sent.",
        )
    return "I've passed your question to the leaders -- I'll send you their answer here."


def is_answer_message(message_text):
    return _ANSWER_PATTERN.match(message_text) is not None


def handle_answer(leader_whatsapp_id, message_text):
    """
    Returns the reply to the answering leader, or None if this isn't an
    exec leader answering a real question (then the message is routed
    normally -- a member typing "answer 3 ..." shouldn't be swallowed).
    """
    leader = get_member_by_whatsapp_id(leader_whatsapp_id)
    match = _ANSWER_PATTERN.match(message_text)
    if not leader or not leader["is_leader"] or not match:
        return None
    question = get_question(int(match.group(1)))
    if not question or question["reg_number"] == leader["reg_number"]:
        return None
    if question["question"] == REMOVED_TEXT:
        return f"The member who asked question {question['id']} has withdrawn from the system, so nothing was sent."
    answer = match.group(2).strip()

    if not record_answer(question["id"], leader["reg_number"], answer, _now()):
        first = get_member_by_reg_number(get_question(question["id"])["answered_by_reg_number"])
        return f"Thanks -- {first['name'] if first else 'another leader'} already answered this one, so nothing more was sent."

    member = get_member_by_reg_number(question["reg_number"])
    if member and member["whatsapp_id"]:
        send_whatsapp_message(
            member["whatsapp_id"],
            f"{leader['name']} answered your question \"{question['question']}\":\n\n{answer}",
        )
    for other in _leaders_except(question["reg_number"]):
        if other["reg_number"] != leader["reg_number"]:
            send_whatsapp_message(other["whatsapp_id"],
                                  f"{leader['name']} has answered question {question['id']} -- no need to reply. Thank you!")
    return "Thanks -- your answer has been sent to them, and the other leaders have been told."


def offer_ask(whatsapp_id, question, feedback_request_id=None):
    start_pending_question_ask(whatsapp_id, question, feedback_request_id, _now())
    return ASK_OFFER


def handle_ask_reply(whatsapp_id, message_text):
    """Returns the reply, or None if this isn't "ASK" (or the offer expired) -- then it's routed normally."""
    pending = get_pending_question_ask(whatsapp_id)
    if pending is None:
        return None
    delete_pending_question_ask(whatsapp_id)

    created_at = pending["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    expired = datetime.now(timezone.utc) - created_at > timedelta(hours=_ASK_EXPIRY_HOURS)
    if expired or message_text.strip().lower().rstrip(".!") != "ask":
        return None

    member = get_member_by_whatsapp_id(whatsapp_id)
    return relay_to_leaders(member, pending["question"], pending["feedback_request_id"])
