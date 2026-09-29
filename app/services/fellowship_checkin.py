"""
app/services/fellowship_checkin.py

Stage 7: the open-fellowship (Monday/Wednesday/Thursday/Friday) side
of reason capture. Unlike Bible Study (a leader confirms who was
absent from a known roster), these are open gatherings with no fixed
roster -- "absence" only makes sense relative to a dynamically
inferred "regular attendee" population, built from a rolling
check-in history, not a pre-registered list.

How the "were you there?" check-in goes out (feedback collection
extended this -- it used to be 9pm only):
  - LEADER-TRIGGERED, preferred: a leader texts the bot near the end
    of the session and the check-in goes out right then, while people
    are still in the room -- fresher answers, and fresher feedback
    after a YES (see feedback.py). Works any day except Tuesday (Bible
    Study uses the group leaders' own absence marking instead) and
    Saturday (nothing runs).
  - 9pm FALLBACK: the original scheduled check-in still runs for each
    tracked fellowship day, but only if no leader already sent one
    that day -- e.g. the fellowship was somewhere without wifi, or the
    leader forgot. checkin_broadcasts' UNIQUE constraint guarantees
    only one of the two ever goes out, even if they race.
  - Noon, every day: sweep any check-in still unanswered from the day
    before. ONLY members who were already a "regular" for that
    specific weekday (checked in for 2 of their last 3 occurrences of
    it) get treated as having lapsed; anyone else is just cleared,
    since we never had grounds to expect them in the first place.

Sunday service is deliberately different -- leader-triggered only, no
9pm fallback, and FEEDBACK ONLY: a YES gets the feedback question, but
a NO never creates an absence, never starts reason capture, and never
reaches the bandit. The proposal explicitly excludes Sunday service
from individual follow-up ("individual follow-up expectations do not
apply"), and asking who attended to collect feedback doesn't change
that.

Live replies are handled immediately, never batched:
  - "yes" (or a close variant) -- check-in recorded, then the feedback
    question in the same reply.
  - a bare "no" with no reason attached -- absence recorded right
    away, then handed to the SAME reason-capture flow Bible Study
    uses (reason_capture.py) to ask why, live, in the same turn.
  - anything else (substantial text, with or without "no") -- treated
    as BOTH the absence signal and the reason in one message, since
    the member already explained themselves unprompted -- asking again
    would be redundant.
"""

from datetime import datetime, timezone, date, timedelta

from app.models.member import get_data_consenting_members, get_member_by_whatsapp_id
from app.models.absence import create_absence
from app.models.fellowship_checkin import record_checkin, count_checkins_on_dates
from app.models.pending_fellowship_checkin import (
    get_pending_fellowship_checkin,
    start_pending_fellowship_checkin,
    delete_pending_fellowship_checkin,
    get_stale_pending_checkins,
)
from app.models.checkin_broadcast import claim_checkin_broadcast, get_checkin_broadcast
from app.services.whatsapp_client import send_whatsapp_message
from app.services.message_generator import display_name_for
from app.services import reason_capture
from app.services import feedback

# weekday: Python's date.weekday() convention (Monday=0 ... Sunday=6).
# Wednesday is specifically prayers, confirmed directly rather than
# assumed generic "fellowship" -- the other three don't have a
# confirmed specific theme, so they stay generically named.
_DAYS = {
    0: "monday_fellowship",
    2: "wednesday_prayers",
    3: "thursday_fellowship",
    4: "friday_fellowship",
}

# Leader-triggerable, but NOT tracked -- see module docstring.
_UNTRACKED_DAYS = {
    6: "sunday_service",
}

_TRACKED_ACTIVITIES = set(_DAYS.values())

# Public -- so app/__init__.py can register the 9pm task for each
# tracked day without reaching into _DAYS directly. Sunday is
# deliberately NOT here: no 9pm fallback for it.
TRACKED_WEEKDAYS = list(_DAYS.keys())

_YES_VARIANTS = {"yes", "yeah", "yep", "yup"}
_BARE_NO_VARIANTS = {"no", "nope", "nah"}

_REGULAR_WINDOW = 3
_REGULAR_THRESHOLD = 2


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return date.today()


def _activity_for_weekday(weekday):
    return _DAYS.get(weekday) or _UNTRACKED_DAYS.get(weekday)


def todays_leader_checkin():
    """
    What a leader-triggered check-in would be for today: the
    activity_type, or None if nothing leader-triggerable runs today
    (Tuesday -- Bible Study has its own marking flow -- or Saturday).
    Inferred from today's date rather than asked, so a leader can't
    accidentally send the wrong day's question.
    """
    return _activity_for_weekday(_today().weekday())


def checkin_already_sent_today(activity_type):
    return get_checkin_broadcast(activity_type, _today()) is not None


def send_fellowship_checkin(weekday, triggered_by="scheduled"):
    """
    Sends today's "were you there?" check-in for `weekday`'s activity
    to every data-consenting, contactable member. Called by the 9pm
    schedule (triggered_by="scheduled", registered once per tracked
    weekday in app/__init__.py) and by a leader's confirmed request
    (triggered_by="leader").

    Returns how many members it was sent to, or None if a check-in for
    this activity already went out today -- claimed atomically first,
    so the 9pm fallback and a leader trigger can never both send.
    """
    activity_type = _activity_for_weekday(weekday)
    today = _today()

    if not claim_checkin_broadcast(activity_type, today, triggered_by, _now()):
        return None

    display_name = display_name_for(activity_type)
    members = get_data_consenting_members()
    # followup_consent too, not just data_consent -- a member who texted
    # STOP was promised no more check-ins, and this IS one.
    contactable = [m for m in members if m["whatsapp_id"] and m["followup_consent"]]

    sent = 0
    for member in contactable:
        message = (
            f"Were you at {display_name} today? Reply YES if you were there, "
            "or let us know what kept you away."
        )
        response = send_whatsapp_message(member["whatsapp_id"], message)
        if response.status_code == 200:
            start_pending_fellowship_checkin(member["whatsapp_id"], activity_type, today, _now())
            sent += 1
    return sent


def send_leader_checkin():
    """
    A leader's confirmed request (see intent_router.py's send_checkin)
    -- sends today's check-in right now. Returns (activity_type, sent):
    activity_type is None if nothing leader-triggerable runs today;
    sent is None if a check-in for today already went out.
    """
    weekday = _today().weekday()
    activity_type = _activity_for_weekday(weekday)
    if activity_type is None:
        return None, None
    return activity_type, send_fellowship_checkin(weekday, triggered_by="leader")


def handle_checkin_message(whatsapp_id, message_text):
    pending = get_pending_fellowship_checkin(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    activity_type = pending["activity_type"]
    checkin_date = pending["checkin_date"]
    display_name = display_name_for(activity_type)
    text = message_text.strip()
    lowered = text.lower().rstrip(".")

    delete_pending_fellowship_checkin(whatsapp_id)
    member = get_member_by_whatsapp_id(whatsapp_id)
    reg_number = member["reg_number"] if member else None

    if lowered in _YES_VARIANTS:
        if not reg_number:
            return "Thanks for letting us know!"
        record_checkin(reg_number, activity_type, checkin_date, _now())
        broadcast = get_checkin_broadcast(activity_type, checkin_date)
        trigger = broadcast["triggered_by"] if broadcast else "scheduled"
        feedback.begin_feedback(whatsapp_id, reg_number, activity_type, checkin_date, trigger)
        return feedback.build_feedback_prompt(display_name, member["name"])

    if activity_type not in _TRACKED_ACTIVITIES:
        # Sunday service -- feedback-only, never an absence. See module docstring.
        return "Thanks for letting us know -- hope to see you next time!"

    if not reg_number:
        return "Thanks for letting us know."

    absence_id = create_absence(reg_number, activity_type, checkin_date, _now())

    if lowered in _BARE_NO_VARIANTS:
        # Reply directly with the "why?" question -- the recipient IS
        # the person currently replying, so this goes through the
        # normal return-based reply path, not a separate direct send.
        reason_capture.begin_reason_capture(whatsapp_id, absence_id)
        return reason_capture.build_live_reason_prompt(member["name"])

    # Substantive text -- already both the absence signal AND the
    # reason in one message, so classify it directly rather than
    # asking a redundant follow-up.
    return reason_capture.classify_and_record(whatsapp_id, absence_id, text)


def _last_n_occurrences(weekday, n, before_date):
    """
    Pure date arithmetic, no DB involved -- the last N calendar dates
    matching `weekday` strictly before `before_date`. Always correct
    regardless of how long the feature's been live: early on, most of
    these dates simply predate it existing, which is exactly why
    count_checkins_on_dates correctly returns low counts during
    cold start instead of wrongly crediting anyone as a "regular" too
    early.
    """
    dates = []
    d = before_date - timedelta(days=1)
    while len(dates) < n:
        if d.weekday() == weekday:
            dates.append(d)
        d -= timedelta(days=1)
    return dates


def is_regular(reg_number, activity_type, weekday, today):
    recent_dates = _last_n_occurrences(weekday, _REGULAR_WINDOW, today)
    count = count_checkins_on_dates(reg_number, activity_type, recent_dates)
    return count >= _REGULAR_THRESHOLD


def process_stale_checkins():
    """
    The scheduled daily-noon task -- sweeps any pending_fellowship_checkin
    row sent on an earlier calendar day, still unanswered. Only members
    already a "regular" for that specific day get treated as lapsed,
    and never for an untracked activity (Sunday service) -- silence
    there is just cleared.
    """
    for row in get_stale_pending_checkins():
        whatsapp_id = row["whatsapp_id"]
        activity_type = row["activity_type"]
        checkin_date = row["checkin_date"]
        weekday = checkin_date.weekday()

        delete_pending_fellowship_checkin(whatsapp_id)

        if activity_type not in _TRACKED_ACTIVITIES:
            continue

        member = get_member_by_whatsapp_id(whatsapp_id)
        if not member or not is_regular(member["reg_number"], activity_type, weekday, checkin_date):
            continue

        # The absence is real attendance data either way; only the
        # "why did you miss it?" message depends on follow-up consent
        # (they may have texted STOP after the check-in went out).
        absence_id = create_absence(member["reg_number"], activity_type, checkin_date, _now())
        if not member["followup_consent"]:
            continue
        message = reason_capture.build_silence_reason_prompt(display_name_for(activity_type), member["name"])
        response = send_whatsapp_message(whatsapp_id, message)
        if response.status_code == 200:
            reason_capture.begin_reason_capture(whatsapp_id, absence_id)
