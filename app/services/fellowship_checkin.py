"""
app/services/fellowship_checkin.py

Stage 7: the open-fellowship (Monday/Wednesday/Thursday/Friday) side
of reason capture. Unlike Bible Study (a leader confirms who was
absent from a known roster), these are open gatherings with no fixed
roster -- "absence" only makes sense relative to a dynamically
inferred "regular attendee" population, built from a rolling
check-in history, not a pre-registered list.

Two scheduled triggers (see scheduler.py):
  - 9pm each day: broadcast a check-in question to every data
    -consenting, contactable member.
  - Noon, every day: sweep any check-in question still unanswered
    from the day before. ONLY members who were already a "regular"
    for that specific weekday (checked in for 2 of their last 3
    occurrences of it) get treated as having lapsed; anyone else is
    just cleared with no follow-up, since we never had grounds to
    expect them in the first place.

Live replies are handled immediately, never batched:
  - "yes" (or a close variant) -- check-in recorded, done.
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
from app.services.whatsapp_client import send_whatsapp_message
from app.services import reason_capture

# weekday: Python's date.weekday() convention (Monday=0 ... Sunday=6).
# Wednesday is specifically prayers, confirmed directly rather than
# assumed generic "fellowship" -- the other three don't have a
# confirmed specific theme, so they stay generically named.
_DAYS = {
    0: ("monday_fellowship", "Monday Fellowship"),
    2: ("wednesday_prayers", "Wednesday Prayers"),
    3: ("thursday_fellowship", "Thursday Fellowship"),
    4: ("friday_fellowship", "Friday Fellowship"),
}

# Public -- so app/__init__.py can register the 9pm task for each
# tracked day without reaching into _DAYS directly.
TRACKED_WEEKDAYS = list(_DAYS.keys())

_YES_VARIANTS = {"yes", "yeah", "yep", "yup"}
_BARE_NO_VARIANTS = {"no", "nope", "nah"}

_REGULAR_WINDOW = 3
_REGULAR_THRESHOLD = 2


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return date.today()


def _reg_number_for(whatsapp_id):
    member = get_member_by_whatsapp_id(whatsapp_id)
    return member["reg_number"] if member else None


def send_fellowship_checkin(weekday):
    """
    The scheduled 9pm task for one specific day -- registered once per
    applicable weekday in app/__init__.py.
    """
    activity_type, display_name = _DAYS[weekday]
    members = get_data_consenting_members()
    contactable = [m for m in members if m["whatsapp_id"]]

    for member in contactable:
        message = (
            f"Were you at {display_name} today? Reply YES if you were there, "
            "or let us know what kept you away."
        )
        response = send_whatsapp_message(member["whatsapp_id"], message)
        if response.status_code == 200:
            start_pending_fellowship_checkin(member["whatsapp_id"], activity_type, _today(), _now())


def handle_checkin_message(whatsapp_id, message_text):
    pending = get_pending_fellowship_checkin(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please message me again."

    activity_type = pending["activity_type"]
    checkin_date = pending["checkin_date"]
    weekday = checkin_date.weekday()
    display_name = _DAYS[weekday][1]
    text = message_text.strip()
    lowered = text.lower().rstrip(".")

    delete_pending_fellowship_checkin(whatsapp_id)
    reg_number = _reg_number_for(whatsapp_id)

    if lowered in _YES_VARIANTS:
        if reg_number:
            record_checkin(reg_number, activity_type, checkin_date, _now())
        return "Thanks for letting us know!"

    if not reg_number:
        return "Thanks for letting us know."

    absence_id = create_absence(reg_number, activity_type, checkin_date, _now())

    if lowered in _BARE_NO_VARIANTS:
        # Reply directly with the "why?" question -- the recipient IS
        # the person currently replying, so this goes through the
        # normal return-based reply path, not a separate direct send.
        reason_capture.begin_reason_capture(whatsapp_id, absence_id)
        return reason_capture.build_reason_prompt(display_name)

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
    already a "regular" for that specific day get treated as lapsed.
    """
    for row in get_stale_pending_checkins():
        whatsapp_id = row["whatsapp_id"]
        activity_type = row["activity_type"]
        checkin_date = row["checkin_date"]
        weekday = checkin_date.weekday()

        delete_pending_fellowship_checkin(whatsapp_id)

        reg_number = _reg_number_for(whatsapp_id)
        if not reg_number or not is_regular(reg_number, activity_type, weekday, checkin_date):
            continue

        absence_id = create_absence(reg_number, activity_type, checkin_date, _now())
        display_name = _DAYS[weekday][1]
        message = reason_capture.build_reason_prompt(display_name)
        response = send_whatsapp_message(whatsapp_id, message)
        if response.status_code == 200:
            reason_capture.begin_reason_capture(whatsapp_id, absence_id)
