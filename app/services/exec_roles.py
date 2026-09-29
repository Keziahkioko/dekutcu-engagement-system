"""
app/services/exec_roles.py

Which member holds which EXEC OFFICE (the Executive Committee, Art. 21).
Added after the first live test: "Who is the chairperson?" could never
be answered, because the system only knew WHETHER someone is a leader
(is_leader), not which office they hold.

A leader-only guided flow records the offices -- the same numbered-list
pattern used everywhere a leader picks a person (area first, then that
area's members; see leader_assignment.py), never free-text name
matching. Meant to be updated each spiritual year after the AGM:
  1. the 11 offices with their current holders, numbered -> pick one
     (or "done");
  2. which area the person lives in -> numbered;
  3. which member in that area -> numbered (or "clear" to leave the
     office vacant);
  4. the person is TOLD they've been recorded in the office
     (transparency), and the office list comes back so several can be
     set in one sitting.
"cancel" stops at any step.

Holding an office MAKES someone a leader (is_leader) -- Keziah's
decision -- and someone who stops holding one loses leader access
automatically, but ONLY if they became a leader through an office
(members.leader_via_office). Leaders made some other way (e.g. Keziah,
a patron) are never touched. Otherwise every past exec would keep
leader powers indefinitely. Everyone affected is told, and so is the
leader recording the change.

Members can ask who holds each office through the groups feature
(group_query's get_exec_committee tool) -- names only; personal numbers
are never shared with members.
"""

from datetime import datetime, timezone

from app.models.member import (
    get_exec_office_holders, set_exec_office, clear_exec_office, get_members_by_area,
)
from app.models.pending_exec_role import (
    get_pending_exec_role, set_pending_exec_role, delete_pending_exec_role,
)
from app.services.registration import AREAS
from app.services.whatsapp_client import send_whatsapp_message

# Art. 21(A), in the constitution's own order and wording.
EXEC_OFFICES = [
    "Chairperson",
    "First Vice Chairperson",
    "Second Vice Chairperson",
    "Finance Secretary",
    "Secretary",
    "Vice Secretary",
    "Missions and Evangelism Director",
    "Prayer Ministry Director",
    "Social Welfare Ministry Director",
    "Discipleship Ministry Director",
    "Music Ministry Director",
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def committee_summary():
    """[(office, holder name or None)] in Art. 21 order -- also the data behind group_query's tool."""
    holders = get_exec_office_holders()
    return [(office, holders[office]["name"] if office in holders else None) for office in EXEC_OFFICES]


def _offices_text():
    lines = [f"{i + 1}. {office} -- {name or '(vacant)'}" for i, (office, name) in enumerate(committee_summary())]
    return (
        "Exec offices:\n\n" + "\n".join(lines)
        + "\n\nReply with the number of the office to set, or 'done' when finished."
    )


def _areas_text():
    return "\n".join(f"{i + 1}. {area}" for i, area in enumerate(AREAS))


def _pick(text, options):
    return options[int(text) - 1] if text.isdigit() and 1 <= int(text) <= len(options) else None


def start(whatsapp_id):
    set_pending_exec_role(whatsapp_id, "awaiting_office", None, None, _now())
    return _offices_text()


def handle_message(whatsapp_id, message_text):
    text = message_text.strip()
    lowered = text.lower()
    pending = get_pending_exec_role(whatsapp_id)
    if pending is None:
        return "Something went wrong on my end -- please start again."

    if lowered in ("cancel", "done"):
        delete_pending_exec_role(whatsapp_id)
        return "Done -- exec offices saved." if lowered == "done" else "Cancelled."

    step = pending["step"]

    if step == "awaiting_office":
        office = _pick(text, EXEC_OFFICES)
        if office is None:
            return f"Please reply with a number from 1 to {len(EXEC_OFFICES)}, or 'done'.\n\n{_offices_text()}"
        set_pending_exec_role(whatsapp_id, "awaiting_area", office, None, _now())
        return (
            f"Who holds {office}? First, which area do they live in?\n\n{_areas_text()}\n\n"
            "Reply with the number -- or 'clear' to leave this office vacant."
        )

    if step == "awaiting_area":
        office = pending["office"]
        if lowered == "clear":
            revoked = clear_exec_office(office)
            set_pending_exec_role(whatsapp_id, "awaiting_office", None, None, _now())
            return f"{office} is now vacant.{_revoked_note(revoked)}\n\n{_offices_text()}"
        area = _pick(text, AREAS)
        if area is None:
            return f"Please reply with a number from 1 to {len(AREAS)}, or 'clear'.\n\n{_areas_text()}"
        return _member_list(whatsapp_id, office, area)

    if step == "awaiting_member":
        office, area = pending["office"], pending["area"]
        candidate = _pick(text, get_members_by_area(area))
        if candidate is None:
            return f"Please reply with a number from the list.\n\n{_member_list(whatsapp_id, office, area)}"
        revoked = set_exec_office(office, candidate["reg_number"])
        if candidate["whatsapp_id"]:
            access = "" if candidate["is_leader"] else " You now also have leader access in the bot."
            send_whatsapp_message(
                candidate["whatsapp_id"],
                f"Hi {candidate['name'].split()[0]}, you've been recorded as DeKUTCU's {office} "
                f"in the CU's system.{access} If this isn't right, please let a leader know.",
            )
        set_pending_exec_role(whatsapp_id, "awaiting_office", None, None, _now())
        return f"Saved: {candidate['name']} is {office}.{_revoked_note(revoked)}\n\n{_offices_text()}"

    delete_pending_exec_role(whatsapp_id)
    return "Something went wrong on my end -- please start again."


def _revoked_note(revoked):
    """Tells each person who lost leader access, and returns a note for the recording leader."""
    for person in revoked:
        if person["whatsapp_id"]:
            send_whatsapp_message(
                person["whatsapp_id"],
                f"Hi {person['name'].split()[0]}, you're no longer recorded as holding an exec office, "
                "so your leader access in the bot has been removed. Thank you for serving! If this "
                "isn't right, please let a leader know.",
            )
    if not revoked:
        return ""
    names = ", ".join(p["name"] for p in revoked)
    return f" {names} no longer hold{'s' if len(revoked) == 1 else ''} an office, so their leader access was removed."


def _member_list(whatsapp_id, office, area):
    members = get_members_by_area(area)
    if not members:
        set_pending_exec_role(whatsapp_id, "awaiting_area", office, None, _now())
        return f"There aren't any registered members in {area}. Pick another area:\n\n{_areas_text()}"
    set_pending_exec_role(whatsapp_id, "awaiting_member", office, area, _now())
    lines = [f"{i + 1}. {m['name']}" for i, m in enumerate(members)]
    return f"Who in {area} is the {office}?\n\n" + "\n".join(lines) + "\n\nReply with the number."
