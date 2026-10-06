"""
app/services/event_manager.py

Stage 6: Event & RSVP Manager.

Two flows, mirroring the guided-conversation pattern used everywhere
else in this project (leader_assignment.py, area_change.py):
  - Leader-side: create_event -- type, title, date, time, location,
    description, then a confirm. On confirm, broadcasts to the
    relevant population and creates the event.
  - Member-side: list upcoming events (a one-shot read, no pending
    state needed), and RSVP to a tracked event (needs pending state --
    a numbered list to disambiguate if more than one tracked event is
    open, same "numbered list, never free-text parsing" pattern used
    for every other selection flow in this project).

"cancel" is a universal escape hatch at any step, same as registration
and leader nomination.
"""

import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from app.models.event import create_event, get_event_by_id, get_upcoming_events
from app.models.event_rsvp import set_rsvp
from app.models.pending_event_creation import (
    get_pending_event_creation,
    start_pending_event_creation,
    update_pending_event_creation,
    delete_pending_event_creation,
)
from app.models.pending_rsvp import (
    get_pending_rsvp,
    start_pending_rsvp,
    update_pending_rsvp,
    delete_pending_rsvp,
)
from app.models.member import get_data_consenting_members
from app.services.whatsapp_client import send_whatsapp_message
from app.services import conversation


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------
# Leader-side: create_event
# --------------------------------------------------------------------

def start_create_event(whatsapp_id):
    start_pending_event_creation(whatsapp_id, _now())
    return (
        "Let's create an event.\n\n"
        "What kind of event is this?\n\n"
        "1. Tracked (Bible Study, cell group, fellowship -- members can RSVP)\n"
        "2. Broadcast (like Sunday service -- announcement only, no RSVP)\n\n"
        "Reply with the number, or 'cancel' to stop."
    )


def handle_create_event_message(whatsapp_id, message_text):
    text = message_text.strip()

    if text.lower() == "cancel":
        delete_pending_event_creation(whatsapp_id)
        return "Event creation cancelled."

    pending = get_pending_event_creation(whatsapp_id)
    if pending is None:
        return start_create_event(whatsapp_id)

    step = pending["step"]

    if step == "awaiting_type":
        return _handle_type(whatsapp_id, text)
    elif step == "awaiting_title":
        return _handle_title(whatsapp_id, text)
    elif step == "awaiting_date":
        return _handle_date(whatsapp_id, text)
    elif step == "awaiting_time":
        return _handle_time(whatsapp_id, text)
    elif step == "awaiting_location":
        return _handle_location(whatsapp_id, text)
    elif step == "awaiting_description":
        return _handle_description(whatsapp_id, text)
    elif step == "awaiting_confirm":
        return _handle_confirm(whatsapp_id, text, pending)

    delete_pending_event_creation(whatsapp_id)
    return "Something went wrong on my end. Let's start over -- message me again."


_TRACKED = {"1", "one", "first", "the first", "first one", "the first one", "tracked", "track", "rsvp", "rsvps",
            "with rsvp", "with rsvps", "tracked event"}
_BROADCAST = {"2", "two", "second", "the second", "second one", "the second one", "broadcast", "announcement",
              "announcement only", "just an announcement", "no rsvp", "no rsvps"}


def _handle_type(whatsapp_id, text):
    # Natural answers count too (Keziah, 2026-10-06: an exact "1" or "2" was the only way through).
    n = conversation.normalise(text)
    if n in _TRACKED:
        event_type = "tracked"
    elif n in _BROADCAST:
        event_type = "broadcast"
    else:
        return (
            "Please reply 1 or 2.\n\n"
            "1. Tracked (members can RSVP)\n"
            "2. Broadcast (announcement only)"
        )
    update_pending_event_creation(whatsapp_id, step="awaiting_title", event_type=event_type)
    return "What's this event called?"


def _handle_title(whatsapp_id, text):
    if not text:
        return "Please enter a title for the event."
    update_pending_event_creation(whatsapp_id, step="awaiting_date", title=text)
    return "What date? (format: YYYY-MM-DD, e.g. 2026-10-05)"


_NAIROBI = ZoneInfo("Africa/Nairobi")
_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"]
_DATE_FORMATS_NO_YEAR = ["%d %b", "%d %B", "%b %d", "%B %d", "%d/%m"]


def parse_event_date(text, today=None):
    """
    A date in the ways leaders actually write it -- 'today', 'tomorrow', '2026-10-05', '5/10/2026',
    '5 Oct', 'October 5' (day/month order, as in Kenya). Without a year: the next such date.
    Returns a date, or None. Fixed 2026-10-01 (QA): only YYYY-MM-DD was accepted, and a past
    date was never refused.
    """
    today = today or datetime.now(_NAIROBI).date()
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip().lower().rstrip("."))
    cleaned = re.sub(r"\s+", " ", cleaned.replace(",", " ")).strip()
    if cleaned == "today":
        return today
    if cleaned == "tomorrow":
        return today + timedelta(days=1)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS_NO_YEAR:
        try:
            parsed = datetime.strptime(f"{cleaned} {today.year}", f"{fmt} %Y").date()
        except ValueError:
            continue
        return parsed if parsed >= today else parsed.replace(year=today.year + 1)
    return None


def _handle_date(whatsapp_id, text):
    event_date = parse_event_date(text)
    if event_date is None:
        return "I didn't recognise that date -- try e.g. 'tomorrow', '5 Oct' or 2026-10-05 (or 'cancel')."
    today = datetime.now(_NAIROBI).date()
    if event_date < today:
        return f"That date ({event_date:%d %b %Y}) has already passed -- please send a date from today onwards."
    if event_date > today + timedelta(days=366):
        return f"That's more than a year away ({event_date:%d %b %Y}) -- please check the date."
    update_pending_event_creation(whatsapp_id, step="awaiting_time", event_date=event_date.isoformat())
    return f"{event_date:%A %d %b %Y} -- got it. What time? (e.g. 5:00 PM)"


def _handle_time(whatsapp_id, text):
    if not text:
        return "Please enter a time, e.g. 5:00 PM."
    update_pending_event_creation(whatsapp_id, step="awaiting_location", event_time=text)
    return "Where's it happening?"


def _handle_location(whatsapp_id, text):
    if not text:
        return "Please enter a location."
    update_pending_event_creation(whatsapp_id, step="awaiting_description", location=text)
    return "Any additional details? Reply 'skip' if none."


def _handle_description(whatsapp_id, text):
    description = None if text.strip().lower() == "skip" else text
    update_pending_event_creation(whatsapp_id, step="awaiting_confirm", description=description)
    pending = get_pending_event_creation(whatsapp_id)
    return _confirmation_text(pending)


def _confirmation_text(pending):
    type_label = (
        "Tracked (members can RSVP)" if pending["event_type"] == "tracked"
        else "Broadcast (announcement only)"
    )
    lines = [
        "Here's what I've got:",
        f"Type: {type_label}",
        f"Title: {pending['title']}",
        f"Date: {pending['event_date']} at {pending['event_time']}",
        f"Location: {pending['location']}",
    ]
    if pending["description"]:
        lines.append(f"Details: {pending['description']}")
    lines.append("\nReply YES to confirm and notify members, or NO to cancel.")
    return "\n".join(lines)


def _handle_confirm(whatsapp_id, text, pending):
    answer = text.strip().lower()
    if answer not in ("yes", "no"):
        return "Please reply YES or NO."

    if answer == "no":
        delete_pending_event_creation(whatsapp_id)
        return "Event creation cancelled."

    event_id = create_event(
        event_type=pending["event_type"],
        title=pending["title"],
        event_date=pending["event_date"],
        event_time=pending["event_time"],
        location=pending["location"],
        description=pending["description"],
        created_by=whatsapp_id,
        created_at=_now(),
    )
    delete_pending_event_creation(whatsapp_id)

    event = get_event_by_id(event_id)
    notified_count = _broadcast_new_event(event)

    return f"Done -- \"{event['title']}\" has been created and {notified_count} member(s) notified."


def _broadcast_new_event(event):
    """
    "tracked" events go to every data-consenting member WITH a group
    placement (confirmed directly: tracked activities are organization
    -wide to everyone placed, not scoped to one group/area).
    "broadcast" events go to every data-consenting member regardless
    of placement -- matches the proposal's explicit rule that members
    without individual follow-up still "remain reachable through
    broadcast announcements".
    """
    members = get_data_consenting_members()

    if event["event_type"] == "tracked":
        recipients = [m for m in members if m["group_label"] is not None and m["whatsapp_id"]]
        message = (
            f"New event: *{event['title']}*\n"
            f"{event['event_date']} at {event['event_time']}\n"
            f"{event['location']}\n"
        )
        if event["description"]:
            message += f"{event['description']}\n"
        message += "\nLet me know if you'll be there!"
    else:
        recipients = [m for m in members if m["whatsapp_id"]]
        message = (
            f"*{event['title']}*\n"
            f"{event['event_date']} at {event['event_time']}\n"
            f"{event['location']}\n"
        )
        if event["description"]:
            message += f"{event['description']}"

    for member in recipients:
        send_whatsapp_message(member["whatsapp_id"], message)

    return len(recipients)


# --------------------------------------------------------------------
# Member-side: list upcoming events (one-shot, no pending state)
# --------------------------------------------------------------------

def list_upcoming_events():
    events = get_upcoming_events()
    if not events:
        return "There aren't any upcoming events right now."

    lines = []
    for e in events:
        line = f"- {e['title']}\n  {e['event_date']} at {e['event_time'] or 'TBA'}, {e['location'] or 'location TBA'}"
        lines.append(line)

    return "Upcoming events:\n\n" + "\n\n".join(lines)


# --------------------------------------------------------------------
# Member-side: RSVP
# --------------------------------------------------------------------

def start_rsvp(whatsapp_id):
    events = get_upcoming_events(event_type="tracked")
    if not events:
        return "There aren't any events open for RSVP right now."

    if len(events) == 1:
        event = events[0]
        start_pending_rsvp(whatsapp_id, "awaiting_response", _now(), event_id=event["id"])
        return f"Will you be at {event['title']} on {event['event_date']}?\n\nReply YES, NO, or MAYBE."

    start_pending_rsvp(whatsapp_id, "choosing_event", _now())
    return "Which event would you like to RSVP for?\n\n" + _events_list_text(events) + "\n\nReply with the number, or 'cancel' to stop."


def _events_list_text(events):
    return "\n".join(f"{i + 1}. {e['title']} ({e['event_date']})" for i, e in enumerate(events))


def handle_rsvp_message(whatsapp_id, message_text):
    text = message_text.strip()

    if text.lower() == "cancel":
        delete_pending_rsvp(whatsapp_id)
        return "No problem, RSVP cancelled."

    pending = get_pending_rsvp(whatsapp_id)
    if pending is None:
        return start_rsvp(whatsapp_id)

    step = pending["step"]

    if step == "choosing_event":
        return _handle_choosing_event(whatsapp_id, text)
    elif step == "awaiting_response":
        return _handle_awaiting_response(whatsapp_id, text, pending)

    delete_pending_rsvp(whatsapp_id)
    return "Something went wrong on my end. Let's start over -- message me again."


def _handle_choosing_event(whatsapp_id, text):
    events = get_upcoming_events(event_type="tracked")
    if not text.isdigit() or not (1 <= int(text) <= len(events)):
        return f"Please reply with a number.\n\n{_events_list_text(events)}"

    event = events[int(text) - 1]
    update_pending_rsvp(whatsapp_id, step="awaiting_response", event_id=event["id"])
    return f"Will you be at {event['title']} on {event['event_date']}?\n\nReply YES, NO, or MAYBE."


_MAYBE = re.compile(r"^(maybe|not sure|perhaps|might|i might|probably|possibly|labda)\b")


def _handle_awaiting_response(whatsapp_id, text, pending):
    # How people actually answer ("yeah I'll come", "sure", "nope", "not sure") -- fixed
    # 2026-10-01 (QA): only the exact words yes/no/maybe used to count.
    answer = "maybe" if _MAYBE.match(conversation.normalise(text)) else conversation.yes_no(text)
    if answer is None:
        return "Please reply YES, NO, or MAYBE (or 'cancel' to stop)."

    event = get_event_by_id(pending["event_id"])
    set_rsvp(pending["event_id"], whatsapp_id, answer, _now())
    delete_pending_rsvp(whatsapp_id)
    return f"Got it -- marked you as {answer.upper()} for {event['title']}. Thanks!"
