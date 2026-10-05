"""
app/services/guide_batches.py

Stage 14 step 4, part 2: batches of printed guides from the Guides
Coordinator to group leaders (design with Keziah 2026-10-05 -- see
PROJECT_LOG.md). The guides are PHYSICAL, so recording a batch is two-sided:

  1. The Coordinator (or the Director while the role is vacant) says "give
     guides to a leader" -> picks the leader from a numbered list (showing
     copies in hand) -> how many -> YES. The batch is recorded as PENDING.
  2. The leader is asked to confirm: "reply RECEIVED 10 (or the number you
     actually got)". Only confirmed copies count toward their stock. A
     different number is recorded as given and flagged to the Coordinator --
     a missing box is caught now, not at the end of the semester.
  3. No confirmation: the leader is reminded after 24 hours; the Coordinator
     is alerted after 3 days.

RECEIVED works at any time (like CLAIM): an unrelated message never cancels
a waiting batch. With several unconfirmed batches, it confirms the oldest.
The Coordinator's conversation follows the usual rules (cancel, a question at
a choice step is handled normally, 30-minute expiry).
"""

import re
from datetime import datetime, timezone, timedelta

from app.database import get_connection
from app.models.member import get_member_by_whatsapp_id, get_member_by_reg_number
from app.models.study_guide import get_current_guide, copies_in_hand
from app.services import conversation, guide_coordinator
from app.services.whatsapp_client import send_whatsapp_message

CONVERSATION_LIFETIME = timedelta(minutes=30)
REMIND_AFTER = timedelta(hours=24)
ALERT_AFTER = timedelta(days=3)
MAX_COPIES = 500
_RECEIVED = re.compile(r"^\s*received\s*(\d+)?\s*(copies)?\s*[.!]?\s*$", re.IGNORECASE)


def init_batch_tables():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_batch (
            whatsapp_id TEXT PRIMARY KEY,
            step TEXT NOT NULL,
            leader_reg_number TEXT,
            copies INTEGER,
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


def can_give_batches(member):
    """The (effective) Guides Coordinator -- the Director while the role is vacant."""
    return guide_coordinator.is_coordinator(member)


def _group_leaders():
    return _q("""SELECT * FROM members WHERE leads_group_label IS NOT NULL AND data_consent
                 ORDER BY leads_group_label, name""")


# --- the Coordinator's conversation ---------------------------------------------------------------

def get_pending(whatsapp_id):
    return _q("SELECT * FROM pending_batch WHERE whatsapp_id = %s", (whatsapp_id,), one=True)


def delete_pending(whatsapp_id):
    _q("DELETE FROM pending_batch WHERE whatsapp_id = %s", (whatsapp_id,))


def _set_pending(whatsapp_id, step, leader=None, copies=None):
    _q("""INSERT INTO pending_batch (whatsapp_id, step, leader_reg_number, copies, created_at)
          VALUES (%s, %s, %s, %s, %s)
          ON CONFLICT (whatsapp_id) DO UPDATE SET step = EXCLUDED.step,
              leader_reg_number = COALESCE(EXCLUDED.leader_reg_number, pending_batch.leader_reg_number),
              copies = COALESCE(EXCLUDED.copies, pending_batch.copies), created_at = EXCLUDED.created_at""",
       (whatsapp_id, step, leader, copies, _now()))


def _leader_list(guide):
    leaders = _group_leaders()
    lines = [f"{i + 1}. {l['name']} ({l['leads_group_label']}) -- {copies_in_hand(l['reg_number'], guide['id'])} in hand"
             for i, l in enumerate(leaders)]
    return leaders, "\n".join(lines)


def begin(member):
    """The give_guide_batch intent."""
    if not can_give_batches(member):
        return "Only the Guides Coordinator records batches of guides."
    guide = get_current_guide()
    if not guide:
        return "There's no study guide on sale right now -- start one first (\"start a new study guide\")."
    leaders, listing = _leader_list(guide)
    if not leaders:
        return "There are no group leaders recorded yet, so there's nobody to give guides to."
    _set_pending(member["whatsapp_id"], "awaiting_leader")
    return f"Which group leader are you giving copies of '{guide['title']}' to?\n\n{listing}\n\nReply with the number (or 'cancel')."


def handle_message(whatsapp_id, message_text):
    """Returns the reply, or None if the conversation expired (then the message is handled normally)."""
    pending = get_pending(whatsapp_id)
    if pending is None:
        return None
    if _now() - pending["created_at"] > CONVERSATION_LIFETIME:
        delete_pending(whatsapp_id)
        return None
    member = get_member_by_whatsapp_id(whatsapp_id)
    guide = get_current_guide()
    if not guide or not can_give_batches(member):
        delete_pending(whatsapp_id)
        return "Nothing was recorded -- the guide or the Coordinator has changed. Please start again."
    n = conversation.normalise(message_text)

    if pending["step"] == "awaiting_leader":
        leaders = _group_leaders()
        if not (n.isdigit() and 1 <= int(n) <= len(leaders)):
            return f"Please reply with a number from 1 to {len(leaders)} (or 'cancel')."
        leader = leaders[int(n) - 1]
        _set_pending(whatsapp_id, "awaiting_copies", leader=leader["reg_number"])
        return f"How many copies are you giving {leader['name']}?"

    if pending["step"] == "awaiting_copies":
        if not (n.isdigit() and 1 <= int(n) <= MAX_COPIES):
            return f"Please reply with a number of copies between 1 and {MAX_COPIES}."
        leader = get_member_by_reg_number(pending["leader_reg_number"])
        _set_pending(whatsapp_id, "awaiting_confirm", copies=int(n))
        return (f"Give {n} {'copy' if n == '1' else 'copies'} of '{guide['title']}' to {leader['name']} "
                f"({leader['leads_group_label']})? They'll be asked to confirm they received them.\n\nReply YES or NO.")

    if pending["step"] == "awaiting_confirm":
        answer = conversation.yes_no(message_text)
        if answer == "no":
            delete_pending(whatsapp_id)
            return "Okay -- nothing was recorded."
        if answer != "yes":
            return "Please reply YES or NO."
        delete_pending(whatsapp_id)
        leader = get_member_by_reg_number(pending["leader_reg_number"])
        if not leader:
            return "That leader is no longer registered, so nothing was recorded."
        copies = pending["copies"]
        _q("""INSERT INTO guide_batches (guide_id, leader_reg_number, copies, given_by_reg_number, given_at, status)
              VALUES (%s, %s, %s, %s, %s, 'pending')""",
           (guide["id"], leader["reg_number"], copies, member["reg_number"], _now()))
        if leader["whatsapp_id"]:
            send_whatsapp_message(leader["whatsapp_id"],
                                  f"{member['name']} is giving you {copies} {'copy' if copies == 1 else 'copies'} of "
                                  f"'{guide['title']}' for your group. When you have them, reply RECEIVED {copies} "
                                  "-- or the number you actually got.")
        return (f"Recorded -- {copies} {'copy' if copies == 1 else 'copies'} for {leader['name']}, waiting for them to "
                "confirm they received them. I'll let you know if they don't.")

    delete_pending(whatsapp_id)
    return None


# --- the leader confirms -------------------------------------------------------------------------------

def is_received_message(text):
    return bool(_RECEIVED.match(text or ""))


def handle_received(leader_whatsapp, message_text):
    """RECEIVED [n] from a group leader. Returns the reply, or None if they have no batch waiting."""
    leader = get_member_by_whatsapp_id(leader_whatsapp)
    match = _RECEIVED.match(message_text or "")
    if not leader or not match:
        return None
    batch = _q("""SELECT b.*, g.title FROM guide_batches b JOIN study_guides g ON g.id = b.guide_id
                  WHERE b.leader_reg_number = %s AND b.status = 'pending' ORDER BY b.given_at LIMIT 1""",
               (leader["reg_number"],), one=True)
    if not batch:
        return None
    got = int(match.group(1)) if match.group(1) else batch["copies"]
    if got > batch["copies"] * 2 + 10:     # almost certainly a typo, not a real count
        return f"That's far more than the {batch['copies']} you were given -- please reply RECEIVED with the number you actually got."
    _q("UPDATE guide_batches SET status = 'confirmed', copies_received = %s, confirmed_at = %s WHERE id = %s",
       (got, _now(), batch["id"]))
    in_hand = copies_in_hand(leader["reg_number"], batch["guide_id"])
    if got != batch["copies"]:
        giver = get_member_by_reg_number(batch["given_by_reg_number"]) or guide_coordinator.get_effective_coordinator()
        if giver and giver["whatsapp_id"]:
            send_whatsapp_message(giver["whatsapp_id"],
                                  f"{leader['name']} ({leader['leads_group_label']}) received {got} "
                                  f"{'copy' if got == 1 else 'copies'} of '{batch['title']}', not the {batch['copies']} recorded.")
    more = _q("SELECT COUNT(*) AS n FROM guide_batches WHERE leader_reg_number = %s AND status = 'pending'",
              (leader["reg_number"],), one=True)["n"]
    extra = f" You have {more} more batch{'es' if more != 1 else ''} to confirm." if more else ""
    note = "" if got == batch["copies"] else " The Coordinator has been told about the difference."
    return f"Thanks -- {got} {'copy' if got == 1 else 'copies'} of '{batch['title']}' recorded. You now have {in_hand} in hand.{note}{extra}"


# --- reminders (every scheduler check) -------------------------------------------------------------

def check_unconfirmed_batches():
    now = _now()
    for b in _q("""SELECT b.*, g.title FROM guide_batches b JOIN study_guides g ON g.id = b.guide_id
                   WHERE b.status = 'pending' AND b.reminded_at IS NULL AND b.given_at < %s""", (now - REMIND_AFTER,)):
        leader = get_member_by_reg_number(b["leader_reg_number"])
        if leader and leader["whatsapp_id"]:
            send_whatsapp_message(leader["whatsapp_id"],
                                  f"Reminder: did you receive the {b['copies']} copies of '{b['title']}'? "
                                  f"Reply RECEIVED {b['copies']} -- or the number you actually got.")
        _q("UPDATE guide_batches SET reminded_at = %s WHERE id = %s", (now, b["id"]))
    for b in _q("""SELECT b.*, g.title FROM guide_batches b JOIN study_guides g ON g.id = b.guide_id
                   WHERE b.status = 'pending' AND b.escalated_at IS NULL AND b.given_at < %s""", (now - ALERT_AFTER,)):
        leader = get_member_by_reg_number(b["leader_reg_number"])
        coordinator = guide_coordinator.get_effective_coordinator()
        if coordinator and coordinator["whatsapp_id"] and leader:
            send_whatsapp_message(coordinator["whatsapp_id"],
                                  f"{leader['name']} ({leader['leads_group_label']}) still hasn't confirmed receiving "
                                  f"{b['copies']} copies of '{b['title']}' (given {b['given_at']:%d %b}). "
                                  "You may want to check with them.")
        _q("UPDATE guide_batches SET escalated_at = %s WHERE id = %s", (now, b["id"]))
