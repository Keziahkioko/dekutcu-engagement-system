"""
app/services/feedback_themes.py

Stage 13 step 3: sorts members' feedback replies into four themes --
in Keziah's own words when she designed feedback collection, members
might send "feedback, or even questions, or recommendations, or
challenges":
  - feedback:       a comment on the session ("worship was powerful but started late")
  - question:       something they want answered ("when is the next retreat?")
  - recommendation: a suggestion ("we should start on time")
  - challenge:      a difficulty in attending or taking part ("the venue is too far from my hostel")

Done as a daily BATCH (2pm, after the noon sweep and the 1pm reward job),
not live -- decided when feedback collection was built, to protect the
Groq budget: a Sunday broadcast can produce a burst of replies at once.
Replies go to the model 20 at a time in ONE call rather than one call
each. Distress-flagged replies are never sorted or shown (the reporting
privacy rule); skips have no text. A reply the model fails to sort just
stays unsorted and is retried the next day -- each run does a fixed
amount of work (up to _MAX_PER_RUN) so one stubborn reply can't loop.

Known gap, logged not built: a QUESTION in feedback is sorted and shown
to leaders in reports, but nobody replies to the member.
"""

import json

from app.models.feedback_request import get_unthemed_feedback, set_theme
from app.services.llm_client import create_chat_completion

THEMES = ["feedback", "question", "recommendation", "challenge"]
_BATCH_SIZE = 20
_MAX_PER_RUN = 200

_SYSTEM_PROMPT = (
    "You sort Christian Union members' feedback replies about a session into exactly one theme each:\n"
    "- feedback: a comment or opinion on the session\n"
    "- question: something they want answered\n"
    "- recommendation: a suggestion for what could be done differently\n"
    "- challenge: a difficulty they face in attending or taking part\n"
    "If a reply mixes several, choose the one it is MAINLY about. Respond with ONLY a JSON object "
    "mapping each reply's number to its theme, e.g. {\"1\": \"feedback\", \"2\": \"question\"}."
)


def _sort_batch(rows):
    numbered = "\n".join(f"{i}. {row['response_text']}" for i, row in enumerate(rows, start=1))
    response = create_chat_completion(
        messages=[{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": numbered}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    themes = json.loads(response.choices[0].message.content)
    sorted_count = 0
    for i, row in enumerate(rows, start=1):
        theme = str(themes.get(str(i), "")).strip().lower()
        if theme in THEMES:  # anything unexpected stays unsorted and is retried tomorrow
            set_theme(row["id"], theme)
            sorted_count += 1
    return sorted_count


def sort_pending_feedback():
    """The daily 2pm scheduled task. Returns how many replies were sorted."""
    rows = get_unthemed_feedback(_MAX_PER_RUN)
    sorted_count = 0
    for start in range(0, len(rows), _BATCH_SIZE):
        try:
            sorted_count += _sort_batch(rows[start:start + _BATCH_SIZE])
        except Exception as e:
            try:
                print(f"Feedback theme batch failed (left for tomorrow): {e}")
            except UnicodeEncodeError:
                print("Feedback theme batch failed (left for tomorrow; error message omitted)")
    return sorted_count
