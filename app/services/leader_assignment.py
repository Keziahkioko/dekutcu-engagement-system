"""
app/services/leader_assignment.py

Stage 6: Recruiting Bible Study group leaders, BEFORE allocation runs.

Real-world process this mirrors: exec leaders informally ask around
about who'd be willing to lead a group in a given area, and once
someone's agreed, the exec leader formalizes it. This module handles
that formalizing step as a guided WhatsApp conversation -- it doesn't
conduct the asking itself (that already happened off-platform, or the
exec leader chooses to have the candidate confirm on-platform instead).

Three steps:
  1. Which area is this for? (numbered list, same AREAS as registration)
  2. Who should lead it? (numbered list of registered members in that
     area by default -- reply "other" to pick a DIFFERENT area to draw
     candidates from instead, for the rare case an area doesn't have
     enough local candidates; leaders can only lead their own residence
     area under normal circumstances, but that's the exec leader's
     judgment call to override, not something this module blocks).
     "other" asks which area to source from rather than dumping every
     registered member into one message -- at real membership size
     that list would blow past WhatsApp's ~4096-character message
     limit (confirmed: 200 members renders past 7000 characters).
  3. Has the candidate already agreed (off-platform), or do they need
     an on-platform confirmation? Bypass assigns immediately; otherwise
     the candidate is messaged directly and THEIR reply is handled
     through the existing pending_actions mechanism in
     intent_router.py, not this module -- a one-shot YES/NO doesn't
     need its own multi-step conversation state.

"cancel" is a universal escape hatch at any step, same as registration.
"""

from datetime import datetime, timezone

from app.models.member import (
    get_members_by_area,
    get_member_by_reg_number,
    nominate_leader,
    assign_leader_directly,
    confirm_leader_nomination,
    remove_leader,
    get_pending_leader_nominees,
    get_confirmed_leaders,
)
from app.models.pending_leader_nomination import (
    get_pending_leader_nomination,
    start_pending_leader_nomination,
    update_pending_leader_nomination,
    delete_pending_leader_nomination,
)
from app.models.pending_action import set_pending_action
from app.services.registration import AREAS
from app.services.whatsapp_client import send_whatsapp_message


def _now():
    return datetime.now(timezone.utc).isoformat()


def _areas_list_text():
    lines = [f"{i + 1}. {area}" for i, area in enumerate(AREAS)]
    return "\n".join(lines)


def start_nomination(whatsapp_id):
    start_pending_leader_nomination(whatsapp_id, _now())
    return (
        "Let's nominate a group leader.\n\n"
        "Which area is this for?\n\n"
        f"{_areas_list_text()}\n\n"
        "Reply with the number, or 'cancel' to stop."
    )


def handle_message(whatsapp_id, message_text):
    """
    Main entry point: advances the nominating leader's in-progress
    conversation and returns the reply text to send back.
    """
    text = message_text.strip()

    if text.lower() == "cancel":
        delete_pending_leader_nomination(whatsapp_id)
        return "Nomination cancelled."

    pending = get_pending_leader_nomination(whatsapp_id)
    if pending is None:
        # Shouldn't normally happen -- webhook.py only calls this when
        # one exists. Fall back to starting fresh just in case.
        return start_nomination(whatsapp_id)

    step = pending["step"]

    if step == "awaiting_area":
        return _handle_area(whatsapp_id, text)
    elif step == "awaiting_candidate":
        return _handle_candidate(whatsapp_id, text, pending)
    elif step == "awaiting_candidate_source_area":
        return _handle_candidate_source_area(whatsapp_id, text, pending)
    elif step == "awaiting_bypass_choice":
        return _handle_bypass_choice(whatsapp_id, text, pending)
    elif step == "resolving_pending":
        return _handle_resolve_pending(whatsapp_id, text)
    elif step == "removing_leader":
        return _handle_remove_leader(whatsapp_id, text)

    delete_pending_leader_nomination(whatsapp_id)
    return "Something went wrong on my end. Let's start over -- message me again."


def _resolve_area(text):
    if text.isdigit() and 1 <= int(text) <= len(AREAS):
        return AREAS[int(text) - 1]
    lowered = text.strip().lower()
    for candidate in AREAS:
        if candidate.lower() == lowered:
            return candidate
    return None


def _handle_area(whatsapp_id, text):
    area = _resolve_area(text)
    if area is None:
        return f"Please reply with a number from 1 to {len(AREAS)}.\n\n{_areas_list_text()}"

    # candidate_scope tracks which area's roster candidates are being
    # drawn FROM -- starts equal to the target area (the default,
    # residency-filtered list) and only changes if "other" is used.
    update_pending_leader_nomination(whatsapp_id, step="awaiting_candidate", area=area, candidate_scope=area)
    return _candidate_list_text(target_area=area, source_area=area)


def _candidate_list_text(target_area, source_area):
    candidates = get_members_by_area(source_area)
    if not candidates:
        return f"There aren't any registered members in {source_area} to choose from right now. Reply 'cancel' to stop, or 'other' to pick a different area."

    lines = [f"{i + 1}. {m['name']}" for i, m in enumerate(candidates)]
    header = f"Who should lead the group in {target_area}?"
    if source_area != target_area:
        header += f" (showing members from {source_area})"

    return (
        f"{header}\n\n" + "\n".join(lines) +
        "\n\nReply with the number, or 'other' to pick a different area to choose from."
    )


def _handle_candidate(whatsapp_id, text, pending):
    target_area = pending["area"]
    source_area = pending["candidate_scope"]
    lowered = text.strip().lower()

    if lowered == "other":
        update_pending_leader_nomination(whatsapp_id, step="awaiting_candidate_source_area")
        return f"Which area should I pull candidates from instead?\n\n{_areas_list_text()}\n\nReply with the number."

    candidates = get_members_by_area(source_area)
    if not text.isdigit() or not (1 <= int(text) <= len(candidates)):
        return f"Please reply with a number.\n\n{_candidate_list_text(target_area, source_area)}"

    candidate = candidates[int(text) - 1]
    update_pending_leader_nomination(
        whatsapp_id, step="awaiting_bypass_choice", candidate_reg_number=candidate["reg_number"]
    )
    return (
        f"Have you already spoken to {candidate['name']} and they agreed to lead the {target_area} group?\n\n"
        "Reply YES to assign them directly, or NO to have me send them a confirmation request first."
    )


def _handle_candidate_source_area(whatsapp_id, text, pending):
    source_area = _resolve_area(text)
    if source_area is None:
        return f"Please reply with a number from 1 to {len(AREAS)}.\n\n{_areas_list_text()}"

    target_area = pending["area"]
    update_pending_leader_nomination(whatsapp_id, step="awaiting_candidate", candidate_scope=source_area)
    return _candidate_list_text(target_area, source_area)


def _handle_bypass_choice(whatsapp_id, text, pending):
    answer = text.strip().lower()
    if answer not in ("yes", "no"):
        return "Please reply YES or NO."

    area = pending["area"]
    candidate = get_member_by_reg_number(pending["candidate_reg_number"])
    delete_pending_leader_nomination(whatsapp_id)

    if answer == "yes":
        assign_leader_directly(candidate["reg_number"], area)
        return f"Done -- {candidate['name']} is now the leader for {area}."

    if not candidate["whatsapp_id"]:
        return (
            f"{candidate['name']} doesn't have a WhatsApp number on file, so I can't send "
            "them a confirmation request. Reply YES on the previous question to assign them "
            "directly instead, or nominate someone else."
        )

    # Send BEFORE persisting any pending state -- if the message never
    # actually reaches the candidate, they shouldn't be left with a
    # phantom pending nomination/action they were never told about,
    # and the exec leader shouldn't be falsely told it worked.
    response = send_whatsapp_message(
        candidate["whatsapp_id"],
        f"You've been asked to lead a Bible Study group in {area}. "
        "Reply YES to accept, or NO to decline."
    )
    if response.status_code != 200:
        return (
            f"Something went wrong sending the confirmation request to {candidate['name']} -- "
            "please try nominating them again."
        )

    nominate_leader(candidate["reg_number"], area, whatsapp_id)
    set_pending_action(candidate["whatsapp_id"], "accept_leader_nomination")
    return f"Message sent to {candidate['name']} -- I'll let you know how they respond."


def _pending_nominees_list_text(nominees):
    lines = [f"{i + 1}. {name} ({area})" for i, (reg_number, name, area) in enumerate(nominees)]
    return "\n".join(lines)


def start_resolve_pending(whatsapp_id):
    """
    For when a candidate accepted OFF-platform (in person, phone call)
    rather than replying to the bot's own confirmation request -- lets
    an exec leader manually mark a pending nomination as accepted.
    """
    nominees = get_pending_leader_nominees()
    if not nominees:
        return "There are no pending leader nominations right now."

    start_pending_leader_nomination(whatsapp_id, _now())
    update_pending_leader_nomination(whatsapp_id, step="resolving_pending")
    return (
        "Who accepted?\n\n" + _pending_nominees_list_text(nominees) +
        "\n\nReply with the number, or 'cancel' to stop."
    )


def _handle_resolve_pending(whatsapp_id, text):
    nominees = get_pending_leader_nominees()
    if not text.isdigit() or not (1 <= int(text) <= len(nominees)):
        return f"Please reply with a number.\n\n{_pending_nominees_list_text(nominees)}"

    reg_number, name, area = nominees[int(text) - 1]
    delete_pending_leader_nomination(whatsapp_id)

    candidate = get_member_by_reg_number(reg_number)
    confirm_leader_nomination(candidate["whatsapp_id"])
    send_whatsapp_message(
        candidate["whatsapp_id"],
        f"You've been confirmed as the leader for {area}. Thank you for accepting!"
    )

    return f"Done -- {name} is now the leader for {area}."


def _confirmed_leaders_list_text(leaders):
    lines = [f"{i + 1}. {name} ({area})" for i, (reg_number, name, area) in enumerate(leaders)]
    return "\n".join(lines)


def start_remove_leader(whatsapp_id):
    leaders = get_confirmed_leaders()
    if not leaders:
        return "There are no group leaders assigned yet."

    start_pending_leader_nomination(whatsapp_id, _now())
    update_pending_leader_nomination(whatsapp_id, step="removing_leader")
    return (
        "Who should be removed as a group leader?\n\n" + _confirmed_leaders_list_text(leaders) +
        "\n\nReply with the number, or 'cancel' to stop."
    )


def _handle_remove_leader(whatsapp_id, text):
    leaders = get_confirmed_leaders()
    if not text.isdigit() or not (1 <= int(text) <= len(leaders)):
        return f"Please reply with a number.\n\n{_confirmed_leaders_list_text(leaders)}"

    reg_number, name, area = leaders[int(text) - 1]
    delete_pending_leader_nomination(whatsapp_id)
    remove_leader(reg_number)
    return f"Done -- {name} is no longer the leader for {area}."
