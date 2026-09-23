"""
app/services/area_change.py

Handles a member updating their own area (the only field of
update_details that's genuinely built -- see intent_router.py). If
they're not yet placed in a group, updating area is all that's needed
-- they'll be picked up by the next top-up run in their new area, same
as any new member.

If they ARE already placed, their existing group_label is deliberately
left untouched (so a routine top-up run doesn't silently move them --
that's the whole reason this isn't just "clear group_label and let
top-up handle it"). Instead a recommendation is computed, reusing the
exact heuristic allocate_members_topup already uses for brand-new
members, and every exec leader gets notified. A leader resolves it via
a numbered list (same non-free-text pattern used everywhere else in
this project) -- approve the recommendation, or pick a different
existing group in the member's new area instead.

Two separate guided conversations live here:
  - The MEMBER'S side: start_area_change / handle_message. One step
    (pick a new area).
  - The LEADER'S side: start_resolve_reassignment /
    handle_resolution_message. Two steps (pick which member's
    reassignment to resolve, then pick which group).
"""

from datetime import datetime, timezone

from app.models.member import (
    get_member_by_whatsapp_id,
    get_member_by_reg_number,
    update_member_area,
    get_members_by_area,
    set_pending_reassignment,
    resolve_reassignment,
    get_pending_reassignments,
    get_all_leaders,
    get_leader_of_group,
)
from app.models.pending_area_change import (
    get_pending_area_change,
    start_pending_area_change,
    delete_pending_area_change,
)
from app.models.pending_reassignment_resolution import (
    get_pending_reassignment_resolution,
    start_pending_reassignment_resolution,
    update_pending_reassignment_resolution,
    delete_pending_reassignment_resolution,
)
from app.services.allocation import recommend_group_for_member
from app.services.registration import AREAS
from app.services.whatsapp_client import send_whatsapp_message


def _now():
    return datetime.now(timezone.utc).isoformat()


def _areas_list_text():
    lines = [f"{i + 1}. {area}" for i, area in enumerate(AREAS)]
    return "\n".join(lines)


def _resolve_area(text):
    if text.isdigit() and 1 <= int(text) <= len(AREAS):
        return AREAS[int(text) - 1]
    lowered = text.strip().lower()
    for candidate in AREAS:
        if candidate.lower() == lowered:
            return candidate
    return None


# ---------------------------------------------------------------------
# Member's side: updating their own area.
# ---------------------------------------------------------------------

def start_area_change(whatsapp_id):
    start_pending_area_change(whatsapp_id, _now())
    return (
        "Which area have you moved to?\n\n"
        f"{_areas_list_text()}\n\n"
        "Reply with the number, or 'cancel' to stop."
    )


def handle_message(whatsapp_id, message_text):
    text = message_text.strip()

    if text.lower() == "cancel":
        delete_pending_area_change(whatsapp_id)
        return "Cancelled -- your area hasn't changed."

    area = _resolve_area(text)
    if area is None:
        return f"Please reply with a number from 1 to {len(AREAS)}.\n\n{_areas_list_text()}"

    delete_pending_area_change(whatsapp_id)
    return _apply_area_change(whatsapp_id, area)


def _apply_area_change(whatsapp_id, new_area):
    member = get_member_by_whatsapp_id(whatsapp_id)
    old_group_label = member["group_label"]

    if old_group_label is None:
        update_member_area(whatsapp_id, new_area)
        return f"Got it -- your area is now {new_area}. You'll be placed into a group there once allocation next runs."

    # Compute the recommendation BEFORE updating their own area -- area_members
    # must not include this member's own (still-stale) row, or their old
    # group_label pollutes the "existing groups in the new area" list.
    area_members = get_members_by_area(new_area)
    updated_member = dict(member)
    updated_member["area"] = new_area
    recommendation = recommend_group_for_member(updated_member, area_members)

    update_member_area(whatsapp_id, new_area)
    set_pending_reassignment(member["reg_number"], recommendation or "")
    _notify_leaders_of_reassignment(member["name"], old_group_label, new_area, recommendation)

    return (
        f"Got it -- your area is now {new_area}. You're still showing as part of "
        f"{old_group_label} for now -- I've sent this to a leader to sort out your "
        "new group, and I'll let you know once it's settled."
    )


def _notify_leaders_of_reassignment(member_name, old_group_label, new_area, recommendation):
    leaders = get_all_leaders()
    if not leaders:
        return

    if recommendation:
        text = (
            f"{member_name} moved from {old_group_label} to {new_area}. "
            f"Recommended: {recommendation}. Reply 'resolve reassignments' to review and act on this."
        )
    else:
        text = (
            f"{member_name} moved from {old_group_label} to {new_area}, but there's no "
            "existing group there to recommend yet. Reply 'resolve reassignments' to review."
        )

    for leader in leaders:
        if leader["whatsapp_id"]:
            send_whatsapp_message(leader["whatsapp_id"], text)


# ---------------------------------------------------------------------
# Leader's side: resolving a pending reassignment.
# ---------------------------------------------------------------------

def _pending_reassignments_list_text(pending):
    lines = [
        f"{i + 1}. {name}: {old_label} -> {new_area}" + (f" (recommended: {rec})" if rec else " (no recommendation yet)")
        for i, (reg_number, name, old_label, new_area, rec) in enumerate(pending)
    ]
    return "\n".join(lines)


def start_resolve_reassignment(whatsapp_id):
    pending = get_pending_reassignments()
    if not pending:
        return "There are no pending reassignments right now."

    start_pending_reassignment_resolution(whatsapp_id, _now())
    return (
        "Which reassignment do you want to resolve?\n\n" + _pending_reassignments_list_text(pending) +
        "\n\nReply with the number, or 'cancel' to stop."
    )


def handle_resolution_message(whatsapp_id, message_text):
    text = message_text.strip()

    if text.lower() == "cancel":
        delete_pending_reassignment_resolution(whatsapp_id)
        return "Cancelled."

    pending = get_pending_reassignment_resolution(whatsapp_id)
    if pending is None:
        return start_resolve_reassignment(whatsapp_id)

    if pending["step"] == "choosing_member":
        return _handle_choosing_member(whatsapp_id, text)
    elif pending["step"] == "choosing_group":
        return _handle_choosing_group(whatsapp_id, text, pending)

    delete_pending_reassignment_resolution(whatsapp_id)
    return "Something went wrong on my end. Let's start over -- message me again."


def _handle_choosing_member(whatsapp_id, text):
    pending_reassignments = get_pending_reassignments()
    if not text.isdigit() or not (1 <= int(text) <= len(pending_reassignments)):
        return f"Please reply with a number.\n\n{_pending_reassignments_list_text(pending_reassignments)}"

    reg_number, name, old_label, new_area, recommendation = pending_reassignments[int(text) - 1]
    update_pending_reassignment_resolution(whatsapp_id, step="choosing_group", member_reg_number=reg_number)
    return _group_options_text(name, new_area, recommendation)


def _existing_groups_in_area(area):
    """
    [(group_label, member_count), ...] for one area, sorted by label.
    Excludes anyone with a pending reassignment -- their area field
    already points here, but their group_label is stale (still their
    OLD area's group), and would otherwise leak a phantom "group" from
    a different area into this list.
    """
    members = get_members_by_area(area)
    counts = {}
    for m in members:
        if m["group_label"] and not m["pending_reassignment"]:
            counts[m["group_label"]] = counts.get(m["group_label"], 0) + 1
    return sorted(counts.items())


def _group_options_text(member_name, new_area, recommendation):
    groups = _existing_groups_in_area(new_area)
    if not groups:
        return (
            f"There aren't any existing groups in {new_area} yet to place {member_name} into -- "
            "they'll need to wait for the next allocation run there. Reply 'cancel' to stop."
        )

    lines = []
    for i, (label, count) in enumerate(groups):
        marker = " (recommended)" if label == recommendation else ""
        lines.append(f"{i + 1}. {label} ({count} members){marker}")

    return (
        f"Which group should {member_name} join in {new_area}?\n\n" + "\n".join(lines) +
        "\n\nReply with the number, or 'cancel' to stop."
    )


def _handle_choosing_group(whatsapp_id, text, pending):
    member = get_member_by_reg_number(pending["member_reg_number"])
    groups = _existing_groups_in_area(member["area"])

    if not text.isdigit() or not (1 <= int(text) <= len(groups)):
        return f"Please reply with a number.\n\n{_group_options_text(member['name'], member['area'], member['pending_reassignment'])}"

    chosen_label, _ = groups[int(text) - 1]
    delete_pending_reassignment_resolution(whatsapp_id)
    resolve_reassignment(member["reg_number"], chosen_label)

    if member["whatsapp_id"]:
        leader = get_leader_of_group(chosen_label)
        leader_note = f", led by {leader['name']}" if leader else ""
        send_whatsapp_message(
            member["whatsapp_id"],
            f"You've been moved to {chosen_label}{leader_note}."
        )

    return f"Done -- {member['name']} is now in {chosen_label}."
