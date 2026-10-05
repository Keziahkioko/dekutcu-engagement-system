"""
app/services/guide_coordinator.py

Stage 14 step 4, part 1: the Guides Coordinator (design revised with Keziah
2026-10-05 -- see PROJECT_LOG.md). One member of the Discipleship team is in
charge of everything to do with the printed study guides: starting the
semester's guide, giving batches to group leaders, and handing guides to
members who have no group leader.

  - Appointed by the Discipleship Ministry Director, who may appoint
    themselves. If no Director is recorded, any exec leader may appoint.
  - Changed at any time the same way (gave up, unavailable, new semester,
    leadership change). The old and new Coordinator are both told. Nothing is
    lost on a change: batches and purchases belong to the GUIDE, not the person.
  - Vacant (never appointed, cleared, or the Coordinator withdrew from the
    system) -> the Director acts as Coordinator; the Director is told when it
    falls vacant unexpectedly.

The conversation: "appoint the guides coordinator" -> 1. Me / 2. Someone else
(area -> member, numbered lists like exec roles) / 3. Leave it vacant -> YES.
Same conversation rules as every flow: cancel always works, a question at a
choice step is handled normally (webhook.py), it expires after 30 minutes.
"""

from datetime import datetime, timezone, timedelta

from app.database import get_connection
from app.models.member import (
    get_member_by_whatsapp_id, get_member_by_reg_number, get_members_by_area, get_exec_office_holders,
)
from app.services import conversation
from app.services.registration import AREAS
from app.services.whatsapp_client import send_whatsapp_message

DIRECTOR_OFFICE = "Discipleship Ministry Director"
CONVERSATION_LIFETIME = timedelta(minutes=30)


def init_guide_coordinator_tables():
    conn = get_connection()
    cursor = conn.cursor()
    # Exactly one row at most -- the primary key can only ever be TRUE.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guide_coordinator (
            id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
            reg_number TEXT NOT NULL,
            appointed_by_reg_number TEXT,
            appointed_at TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_coordinator_choice (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            area TEXT,
            candidate_reg_number TEXT,
            created_at TIMESTAMP NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def _q(sql, params=(), one=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall() if cursor.description else None
    conn.commit()
    cursor.close()
    conn.close()
    return (rows[0] if rows else None) if one else rows


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- who holds what --------------------------------------------------------------------------------

def get_director():
    return get_exec_office_holders().get(DIRECTOR_OFFICE)


def get_appointed_coordinator():
    """The appointed Coordinator (a member row), or None if the role is vacant."""
    row = _q("SELECT reg_number FROM guide_coordinator", one=True)
    if not row:
        return None
    member = get_member_by_reg_number(row["reg_number"])
    return member if member and member["data_consent"] else None


def get_effective_coordinator():
    """Who actually handles guides right now: the appointed Coordinator, else the Director (acting)."""
    return get_appointed_coordinator() or get_director()


def is_coordinator(member):
    effective = get_effective_coordinator()
    return bool(member and effective and effective["reg_number"] == member["reg_number"])


def can_appoint(member):
    """The Discipleship Ministry Director; any exec leader only when no Director is recorded."""
    if not member:
        return False
    director = get_director()
    if director:
        return director["reg_number"] == member["reg_number"]
    return bool(member["is_leader"])


def can_manage_guides(member):
    """May start a guide (and, in later parts, record batches): the Coordinator, the Director, or an exec leader."""
    return bool(member) and (is_coordinator(member) or bool(member["is_leader"]) or
                             (get_director() is not None and get_director()["reg_number"] == member["reg_number"]))


# --- setting it -------------------------------------------------------------------------------------

def set_coordinator(new_reg_number, appointed_by):
    """Appoints (or, with None, clears). Tells the old and new Coordinator. Returns the previous holder (or None)."""
    previous = get_appointed_coordinator()
    if new_reg_number is None:
        _q("DELETE FROM guide_coordinator")
    else:
        _q("""INSERT INTO guide_coordinator (id, reg_number, appointed_by_reg_number, appointed_at)
              VALUES (TRUE, %s, %s, %s)
              ON CONFLICT (id) DO UPDATE SET reg_number = EXCLUDED.reg_number,
                  appointed_by_reg_number = EXCLUDED.appointed_by_reg_number, appointed_at = EXCLUDED.appointed_at""",
           (new_reg_number, appointed_by["reg_number"], _now()))
    new = get_member_by_reg_number(new_reg_number) if new_reg_number else None
    if previous and (not new or previous["reg_number"] != new["reg_number"]) and previous["whatsapp_id"]:
        send_whatsapp_message(previous["whatsapp_id"],
                              f"You're no longer the Guides Coordinator -- {new['name'] if new else 'the Director'} "
                              "is taking over the study guides. Thank you for serving! 🙏")
    if new and new["reg_number"] != appointed_by["reg_number"] and new["whatsapp_id"]:
        send_whatsapp_message(new["whatsapp_id"],
                              f"{appointed_by['name']} has made you DeKUTCU's Guides Coordinator. You're in charge of the "
                              "study guides: starting each semester's guide, giving batches to group leaders, and "
                              "handing guides to members who have no group leader. Say \"help\" any time to see how.")
    return previous


def on_member_leaving(reg_number):
    """
    Called when a member's record is about to be removed (consent withdrawal): if they were the Coordinator,
    the role falls vacant and the Director is told (they act as Coordinator meanwhile).
    """
    row = _q("SELECT reg_number FROM guide_coordinator", one=True)
    if not row or row["reg_number"] != reg_number:
        return
    _q("DELETE FROM guide_coordinator")
    director = get_director()
    if director and director["whatsapp_id"] and director["reg_number"] != reg_number:
        send_whatsapp_message(director["whatsapp_id"],
                              "The Guides Coordinator role is now vacant (they've left the system). Until you appoint "
                              "someone, you're acting as Coordinator -- say \"appoint the guides coordinator\" when ready.")


# --- the conversation -----------------------------------------------------------------------------

def get_pending(whatsapp_id):
    return _q("SELECT * FROM pending_coordinator_choice WHERE whatsapp_id = %s", (whatsapp_id,), one=True)


def _set_pending(whatsapp_id, step, area=None, candidate=None):
    _q("""INSERT INTO pending_coordinator_choice (whatsapp_id, step, area, candidate_reg_number, created_at)
          VALUES (%s, %s, %s, %s, %s)
          ON CONFLICT (whatsapp_id) DO UPDATE SET step = EXCLUDED.step, area = EXCLUDED.area,
              candidate_reg_number = EXCLUDED.candidate_reg_number, created_at = EXCLUDED.created_at""",
       (whatsapp_id, step, area, candidate, _now()))


def delete_pending(whatsapp_id):
    _q("DELETE FROM pending_coordinator_choice WHERE whatsapp_id = %s", (whatsapp_id,))


def begin(member):
    """The appoint_guides_coordinator intent."""
    if not can_appoint(member):
        director = get_director()
        return (f"Only the Discipleship Ministry Director ({director['name']}) can appoint the Guides Coordinator."
                if director else "Only an exec leader can appoint the Guides Coordinator.")
    current = get_appointed_coordinator()
    now = f"The current Guides Coordinator is {current['name']}." if current else "There's no Guides Coordinator at the moment."
    _set_pending(member["whatsapp_id"], "awaiting_choice")
    return (f"{now} Who should it be?\n\n"
            f"1. Me ({member['name']})\n2. Someone else\n3. Nobody for now (the Director handles guides)\n\n"
            "Reply 1, 2 or 3 (or 'cancel').")


def _pick(text, items):
    n = conversation.normalise(text)
    return items[int(n) - 1] if n.isdigit() and 1 <= int(n) <= len(items) else None


def _confirm(whatsapp_id, candidate):
    _set_pending(whatsapp_id, "awaiting_confirm", candidate=candidate["reg_number"] if candidate else "")
    who = f"{candidate['name']}" if candidate else "nobody (the Director handles guides meanwhile)"
    return f"Make {who} the Guides Coordinator? Reply YES or NO."


def handle_message(whatsapp_id, message_text):
    """Returns the reply, or None if the conversation expired (then the message is handled normally)."""
    pending = get_pending(whatsapp_id)
    if pending is None:
        return None
    if _now() - pending["created_at"] > CONVERSATION_LIFETIME:
        delete_pending(whatsapp_id)
        return None
    member = get_member_by_whatsapp_id(whatsapp_id)
    if not can_appoint(member):     # lost the right meanwhile (e.g. a Director was recorded)
        delete_pending(whatsapp_id)
        return "Only the Discipleship Ministry Director can appoint the Guides Coordinator now, so nothing was changed."
    step, text = pending["step"], message_text.strip()

    if step == "awaiting_choice":
        n = conversation.normalise(text)
        if n in ("1", "me", "myself"):
            return _confirm(whatsapp_id, member)
        if n in ("2", "someone else", "someone"):
            _set_pending(whatsapp_id, "awaiting_area")
            areas = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(AREAS))
            return f"Which area do they live in?\n\n{areas}\n\nReply with the number."
        if n in ("3", "nobody", "none", "vacant"):
            return _confirm(whatsapp_id, None)
        return "Please reply 1 (me), 2 (someone else) or 3 (nobody for now) -- or 'cancel'."

    if step == "awaiting_area":
        area = _pick(text, AREAS)
        if area is None:
            return f"Please reply with a number from 1 to {len(AREAS)}."
        members = get_members_by_area(area)
        if not members:
            return f"There aren't any registered members in {area}. Please pick another area (1-{len(AREAS)})."
        _set_pending(whatsapp_id, "awaiting_member", area=area)
        return f"Who in {area}?\n\n" + "\n".join(f"{i + 1}. {m['name']}" for i, m in enumerate(members)) + "\n\nReply with the number."

    if step == "awaiting_member":
        candidate = _pick(text, get_members_by_area(pending["area"]))
        if candidate is None:
            return "Please reply with a number from the list (or 'cancel')."
        return _confirm(whatsapp_id, candidate)

    if step == "awaiting_confirm":
        answer = conversation.yes_no(text)
        if answer == "no":
            delete_pending(whatsapp_id)
            return "Okay -- nothing was changed."
        if answer != "yes":
            return "Please reply YES or NO."
        delete_pending(whatsapp_id)
        reg = pending["candidate_reg_number"] or None
        set_coordinator(reg, member)
        if reg is None:
            return "Done -- there's no Guides Coordinator now; the Director handles the study guides meanwhile."
        new = get_member_by_reg_number(reg)
        return ("Done -- you're now the Guides Coordinator." if reg == member["reg_number"]
                else f"Done -- {new['name']} is now the Guides Coordinator, and they've been told.")

    delete_pending(whatsapp_id)
    return None
