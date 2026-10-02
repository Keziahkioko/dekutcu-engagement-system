"""
app/services/conversation.py

Shared rules for reading short replies inside multi-step conversations
(added 2026-10-01 after QA testing showed most flows could TRAP a
student: they re-asked forever unless the reply was an exact word, and
only the exact word "cancel" got out -- one trapped confirmation even
turned a later, unrelated "yes" into a permanent consent withdrawal).

Every flow uses the same three questions, so behaviour is consistent:
  - is_cancel: does the student want out ("cancel", "never mind",
    "start over", "back", "menu"...)?
  - yes_no: is this a yes or a no, in the ways people actually write it
    ("yeah", "sure", "nope", "ndio", "yes please", "yeah I'll come")?
  - looks_like_new_request: at a step expecting a structured answer (a
    number, a date, yes/no), does this read like a NEW message instead
    (a question, or a sentence)? Then the flow is dropped and the
    message is handled normally -- never swallowed. Never used at steps
    that expect free text (a title, a name), where sentences are normal.

STOP is deliberately NOT a cancel word: it's the global, keyword-based
opt-out from check-ins (see webhook.py), which must never depend on
context or on an AI classifier.
"""

import re

_CANCEL = {
    "cancel", "cancel that", "cancel it", "cancel everything", "never mind", "nevermind", "nvm",
    "forget it", "forget that", "forget what i said", "start over", "start again", "restart",
    "back", "go back", "previous", "main menu", "menu", "exit", "quit", "abort", "leave it",
}
_YES = {"yes", "y", "yeah", "yea", "ya", "yep", "yup", "sure", "ok", "okay", "k", "alright", "confirm",
        "go ahead", "yes please", "ndio", "ndiyo", "sawa", "of course", "definitely", "absolutely", "correct"}
_NO = {"no", "n", "nope", "nah", "no thanks", "no thank you", "hapana", "not really", "don't", "dont"}
_YES_FIRST_WORDS = {"yes", "yeah", "yea", "yep", "yup", "sure", "ndio", "ndiyo", "okay", "ok"}
_NO_FIRST_WORDS = {"no", "nope", "nah", "hapana"}
_QUESTION_START = re.compile(r"^(what|who|where|when|why|how|which|is|are|can|could|do|does|will|would|should|tell|show|give|explain|i want|i need|i'd like)\b")


def normalise(text):
    """Lower-cased, trimmed, without surrounding punctuation or emoji -- for matching short replies."""
    cleaned = (text or "").strip().lower()
    cleaned = re.sub(r"^[^\w]+|[^\w']+$", "", cleaned)
    return re.sub(r"\s+", " ", cleaned)


def is_cancel(text):
    n = normalise(text)
    return n in _CANCEL or n.startswith("cancel ")


def yes_no(text):
    """'yes', 'no', or None. A reply that STARTS with a clear yes/no counts ('yeah I'll come', 'no, I was sick')."""
    n = normalise(text)
    if n in _YES:
        return "yes"
    if n in _NO:
        return "no"
    first = re.split(r"[\s,.!]+", n)[0] if n else ""
    if first in _YES_FIRST_WORDS:
        return "yes"
    if first in _NO_FIRST_WORDS:
        return "no"
    return None


def strict_yes_no(text):
    """For CONSENT: only a clear yes or no on its own ('yeah', 'sure', 'nope'...) -- not 'yes, but...'."""
    n = normalise(text)
    return "yes" if n in _YES else "no" if n in _NO else None


def looks_like_new_request(text):
    """At a STRUCTURED step only: a question or a sentence rather than the expected short answer."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if "?" in stripped:
        return True
    words = normalise(stripped).split()
    return len(words) >= 4 or bool(words and _QUESTION_START.match(" ".join(words)))


HELP_TEXT = (
    "Here's what I can help with:\n"
    "• What DeKUTCU believes and how it's run (just ask)\n"
    "• Your Bible Study group and who leads it\n"
    "• Upcoming events -- and RSVPing to them\n"
    "• Buying this semester's study guide\n"
    "• Sharing feedback, or asking a question for the leaders\n"
    "• Connecting you with a leader, or DeKUTCU's contact details\n\n"
    "Just type what you need in your own words. (Text STOP to pause check-ins.)"
)

_HELP_WORDS = {"help", "menu", "main menu", "start", "/start", "options", "what can you do",
               "what can you help with", "what do you do", "commands", "how does this work",
               "who are you", "what are you", "what is this", "what is this bot"}


def is_help_request(text):
    return normalise(text) in _HELP_WORDS
