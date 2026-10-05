"""
app/services/number_change.py

Moving a member's account to a new WhatsApp number -- SAFELY (2026-10-05,
design settled with Keziah). Found in the QA pass (BUG-02, critical):
registration moved an account to any new phone that typed an existing
member's registration number. Reg numbers aren't secret (class lists), so
anyone could take over anyone's account -- a leader's included (reports
with members' names, announcements to everyone) -- and lock the owner out.

People change numbers often, so a genuine member must never be stuck.
Four ways a move is confirmed:

  1. Told in advance -- from the OLD number the member says "I'm changing
     my number" and gives the new one. That message proves it's them, so
     when the new number first messages the bot, the account moves at once.
  2. The old number confirms -- a new phone types the reg number; the old
     number is asked "is this you? YES/NO".
  3. A leader confirms -- when the old number can't answer: WhatsApp
     reports the message undeliverable (they used WhatsApp's "Change
     number", or the SIM is gone), the account has no number on file, or
     there's no reply within 24 hours. Their group leader (or the exec
     leaders) gets APPROVE/DENY and checks in person or by a call.
  4. Leader accounts (exec or group leaders) -- the most valuable to take
     over -- ALWAYS need an exec leader's approval too, whatever the old
     number says.

Nothing moves until confirmed. Limits: one open request per account; 3
attempts per new number per day; requests expire (old number 24h ->
leader; leader 3 days; told-in-advance 7 days).

Rollout note: the message to the old number is bot-initiated, so in
production it needs a WhatsApp template (Meta's 24-hour rule).
"""

import re
from datetime import datetime, timezone, timedelta

from app.database import get_connection
from app.models.member import get_member_by_whatsapp_id, get_member_by_reg_number, get_all_leaders, relink_whatsapp_id
from app.models.pending_action import set_pending_action, clear_pending_action, get_pending_action
from app.services import conversation, mpesa, org_contacts
from app.services.whatsapp_client import send_whatsapp_message

OLD_NUMBER_WINDOW = timedelta(hours=24)
LEADER_WINDOW = timedelta(days=3)
ADVANCE_WINDOW = timedelta(days=7)
ASK_WINDOW = timedelta(minutes=30)
MAX_ATTEMPTS_PER_DAY = 3
_OPEN = ("advance", "awaiting_old", "awaiting_leader")
_DECISION = re.compile(r"^\s*(approve|deny)\s+(\d+)\s*[.!]?\s*$", re.IGNORECASE)


def init_number_change_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS number_changes (
            id SERIAL PRIMARY KEY,
            reg_number TEXT NOT NULL,
            old_whatsapp TEXT,
            new_whatsapp TEXT,
            status TEXT NOT NULL,
            leader_account BOOLEAN NOT NULL DEFAULT FALSE,
            old_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
            old_message_id TEXT,
            notified_leaders TEXT,
            decided_by TEXT,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _q(sql, params=(), one=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall() if cursor.description else None
    conn.commit()
    cursor.close()
    conn.close()
    return (rows[0] if rows else None) if one else rows


def _update(row_id, **fields):
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = %s" for k in fields)
    return _q(f"UPDATE number_changes SET {sets} WHERE id = %s RETURNING *", (*fields.values(), row_id), one=True)


def _mask(number):
    digits = re.sub(r"\D", "", number or "")
    return f"ending ...{digits[-3:]}" if len(digits) >= 3 else "on file"


def _first(member):
    return (member["name"] or "there").split()[0]


def _is_leader_account(member):
    return bool(member["is_leader"] or member["leads_group_label"])


def _parse_number(text):
    """A WhatsApp number in Meta's format (country code, digits only): Kenyan forms via mpesa, else +<country><number>."""
    kenyan = mpesa.normalise_phone(text)
    if kenyan:
        return kenyan
    digits = re.sub(r"[\s\-()]", "", text or "")
    if digits.startswith("+") and digits[1:].isdigit() and 10 <= len(digits[1:]) <= 15:
        return digits[1:]
    return None


def _send(to, text):
    """Sends; returns Meta's message id, or None if the send itself failed."""
    try:
        response = send_whatsapp_message(to, text)
    except Exception as e:
        print(f"Number-change message failed: {e}")
        return None
    if getattr(response, "status_code", 200) != 200:
        return None
    try:
        return response.json()["messages"][0]["id"]
    except Exception:
        return "sent"


# --- 1. a new phone types an existing member's reg number (called from registration) ------------

def request_move(member, new_whatsapp):
    """Returns the reply to the NEW number. Nothing moves until confirmed."""
    recent = _q("""SELECT COUNT(*) AS n FROM number_changes
                   WHERE new_whatsapp = %s AND created_at > %s""", (new_whatsapp, _now() - timedelta(days=1)), one=True)["n"]
    if recent >= MAX_ATTEMPTS_PER_DAY:
        return ("Too many attempts from this number today. Please try again tomorrow, "
                "or ask your group leader to help.")

    open_row = _q(f"""SELECT * FROM number_changes WHERE reg_number = %s AND status IN %s ORDER BY id DESC LIMIT 1""",
                  (member["reg_number"], _OPEN), one=True)
    if open_row and open_row["status"] == "advance" and open_row["new_whatsapp"] == new_whatsapp:
        return _advance_arrived(open_row)
    if open_row:
        return ("There's already a request to move that account to a new number. Please wait for it to be "
                "confirmed, or ask your group leader to help.")

    row = _q("""INSERT INTO number_changes (reg_number, old_whatsapp, new_whatsapp, status, leader_account, created_at, updated_at)
                VALUES (%s, %s, %s, 'awaiting_old', %s, %s, %s) RETURNING *""",
             (member["reg_number"], member["whatsapp_id"], new_whatsapp, _is_leader_account(member), _now(), _now()), one=True)

    if not member["whatsapp_id"]:
        return _to_leaders(row, "There's no WhatsApp number on file for them.")

    message_id = _send(member["whatsapp_id"],
                       f"Hi {_first(member)}, someone is moving your DeKUTCU account to a new WhatsApp number "
                       f"{_mask(new_whatsapp)}. Is this you?\n\nReply YES to move it, or NO if it isn't you.")
    if not message_id:
        return _to_leaders(row, "Their old number couldn't be reached.")
    _update(row["id"], old_message_id=message_id)
    set_pending_action(member["whatsapp_id"], "confirm_number_change")
    return ("That registration number already belongs to a member. To protect their account, I've sent a "
            f"confirmation to the number on file ({_mask(member['whatsapp_id'])}). Once it's confirmed, you'll be "
            "all set here -- I'll message you.")


# --- 2. the old number answers ------------------------------------------------------------------

def handle_old_reply(old_whatsapp, answer):
    """Called from the YES/NO confirmation handler. Returns the reply to the OLD number."""
    row = _q("SELECT * FROM number_changes WHERE old_whatsapp = %s AND status = 'awaiting_old' ORDER BY id DESC LIMIT 1",
             (old_whatsapp,), one=True)
    if not row:
        return "Thanks -- that request has already been dealt with, so nothing has changed."
    if answer == "no":
        _update(row["id"], status="denied", decided_by="old_number")
        _send(row["new_whatsapp"], "The member's number on file said this request wasn't them, so the account "
                                   "wasn't moved. If this is a mistake, please talk to your group leader.")
        return ("Okay -- your account stays on this number. If you didn't expect this, someone may know your "
                "registration number; you might want to let a leader know.")
    row = _update(row["id"], old_confirmed=True)
    if row["leader_account"]:
        _to_leaders(row, "Their old number has confirmed it, but leader accounts need an exec leader's approval too.")
        return ("Thanks! Because this is a leader account, an exec leader will also confirm before it moves -- "
                "you'll both be told once it's done.")
    _apply(row, "old_number")
    return "Thanks -- your account has been moved to your new number. 👍"


def handle_delivery_failure(message_id):
    """WhatsApp reported a message undeliverable (webhook 'statuses'): the old number can't answer -> a leader."""
    if not message_id:
        return
    row = _q("SELECT * FROM number_changes WHERE old_message_id = %s AND status = 'awaiting_old'", (message_id,), one=True)
    if not row:
        return
    if row["old_whatsapp"]:
        clear_pending_action(row["old_whatsapp"])
    _send(row["new_whatsapp"], _to_leaders(row, "Their old number can't be reached on WhatsApp."))


# --- 3. leaders approve ------------------------------------------------------------------------

def _approvers(member, leader_account):
    if leader_account:   # leader accounts: exec leaders only
        return [l for l in get_all_leaders() if l["whatsapp_id"] and l["reg_number"] != member["reg_number"]]
    from app.services.escalation import find_target_leaders
    regs = [reg for _, _, reg in find_target_leaders(member)]
    return [get_member_by_reg_number(r) for r in regs]


def _to_leaders(row, reason):
    """Asks the leader(s) to confirm. Returns the reply/notice for the NEW number."""
    member = get_member_by_reg_number(row["reg_number"])
    leaders = [l for l in _approvers(member, row["leader_account"]) if l and l["whatsapp_id"]]
    if not leaders:
        _update(row["id"], status="awaiting_leader", notified_leaders="")
        contacts = org_contacts.contact_reply()
        return ("I couldn't reach the number on file, and there's no leader I can ask to confirm it's you. "
                f"Please contact DeKUTCU directly:\n\n{contacts}")
    _update(row["id"], status="awaiting_leader", notified_leaders=",".join(l["reg_number"] for l in leaders))
    for leader in leaders:
        _send(leader["whatsapp_id"],
              f"{member['name']} ({member['reg_number']}) wants to move their DeKUTCU account to a new number "
              f"{_mask(row['new_whatsapp'])}. {reason}\n\nIf you've confirmed it's really them (in person or by a call), "
              f"reply APPROVE {row['id']} -- or DENY {row['id']} if it isn't.")
    who = (f"{leaders[0]['name']}, your group leader" if len(leaders) == 1 and not row["leader_account"]
           else "the exec leaders")
    return f"I couldn't confirm it from the number on file, so I've asked {who} to confirm it's you. I'll message you once it's done."


def is_decision_message(text):
    return bool(_DECISION.match(text or ""))


def handle_decision(leader_whatsapp, text):
    """APPROVE n / DENY n from a leader. Returns the reply, or None if this sender has nothing to decide."""
    leader = get_member_by_whatsapp_id(leader_whatsapp)
    match = _DECISION.match(text or "")
    if not leader or not match:
        return None
    row = _q("SELECT * FROM number_changes WHERE id = %s", (int(match.group(2)),), one=True)
    if not row or leader["reg_number"] not in (row["notified_leaders"] or "").split(","):
        return None
    if row["status"] != "awaiting_leader":
        return f"Request {row['id']} has already been dealt with."
    member = get_member_by_reg_number(row["reg_number"])
    if match.group(1).lower() == "deny":
        _update(row["id"], status="denied", decided_by=leader["reg_number"])
        _send(row["new_whatsapp"], "A leader couldn't confirm this number belongs to that member, so the account "
                                   "wasn't moved. Please talk to your group leader if this is a mistake.")
        return f"Okay -- {member['name']}'s account stays where it is."
    result = _apply(row, leader["reg_number"])
    return result or f"Done -- {member['name']}'s account is now on their new number, and they've been told."


# --- moving it -------------------------------------------------------------------------------------

def _welcome(member):
    return f"Your DeKUTCU account is now on this number. Welcome back, {_first(member)}! 🎉"


def _apply(row, decided_by, tell_new_number=True):
    """
    Moves the account. Returns an error message if it couldn't, else None. tell_new_number=False when the
    new number is the one messaging right now (the caller returns the welcome as the reply instead).
    """
    if get_member_by_whatsapp_id(row["new_whatsapp"]):
        _update(row["id"], status="denied", decided_by=decided_by)
        return "That new number is already registered to another member, so the account wasn't moved."
    member = get_member_by_reg_number(row["reg_number"])
    old = member["whatsapp_id"]
    relink_whatsapp_id(member["reg_number"], row["new_whatsapp"])
    _q("DELETE FROM pending_registrations WHERE whatsapp_id = %s", (row["new_whatsapp"],))
    if old:   # their RSVPs and any events they created follow them
        _q("UPDATE event_rsvps SET whatsapp_id = %s WHERE whatsapp_id = %s", (row["new_whatsapp"], old))
        _q("UPDATE events SET created_by = %s WHERE created_by = %s", (row["new_whatsapp"], old))
        if get_pending_action(old) and get_pending_action(old)["action"] == "confirm_number_change":
            clear_pending_action(old)
    _update(row["id"], status="approved", decided_by=decided_by)
    if tell_new_number:
        _send(row["new_whatsapp"], _welcome(member))
    if old and decided_by != "old_number":
        _send(old, f"Your DeKUTCU account has been moved to your new number {_mask(row['new_whatsapp'])}.")
    return None


# --- 4. told in advance ("I'm changing my number") ---------------------------------------------------

def begin_change_number(member):
    _q("DELETE FROM number_changes WHERE old_whatsapp = %s AND status = 'asking'", (member["whatsapp_id"],))
    _q("""INSERT INTO number_changes (reg_number, old_whatsapp, status, leader_account, created_at, updated_at)
          VALUES (%s, %s, 'asking', %s, %s, %s)""",
       (member["reg_number"], member["whatsapp_id"], _is_leader_account(member), _now(), _now()))
    return ("Sure -- what's your new WhatsApp number? (e.g. 0712 345 678, or +44... for a foreign number). "
            "Or reply 'cancel'.")


def get_asking(whatsapp_id):
    return _q("SELECT * FROM number_changes WHERE old_whatsapp = %s AND status = 'asking'", (whatsapp_id,), one=True)


def handle_new_number_reply(whatsapp_id, text):
    """Returns the reply, or None (question dropped, message handled normally)."""
    row = get_asking(whatsapp_id)
    if not row:
        return None
    if _now() - row["created_at"] > ASK_WINDOW or conversation.is_cancel(text):
        _q("DELETE FROM number_changes WHERE id = %s", (row["id"],))
        return "Okay -- nothing has changed." if conversation.is_cancel(text) else None
    new = _parse_number(text)
    if not new:
        if conversation.looks_like_new_request(text):
            _q("DELETE FROM number_changes WHERE id = %s", (row["id"],))
            return None
        return "That doesn't look like a phone number -- please send it like 0712 345 678 (or 'cancel')."
    if new == whatsapp_id:
        return "That's the number you're messaging me from already -- what's the NEW number? (or 'cancel')"
    if get_member_by_whatsapp_id(new):
        _q("DELETE FROM number_changes WHERE id = %s", (row["id"],))
        return "That number is already registered to another member, so I can't move your account to it."
    _update(row["id"], status="advance", new_whatsapp=new, old_confirmed=True)
    extra = (" Because you're a leader, an exec leader will also confirm it then." if row["leader_account"] else "")
    return (f"Got it. When you message me from your new number ({_mask(new)}), your account will move there "
            f"straight away.{extra} This stays ready for 7 days.")


def on_unregistered_message(whatsapp_id):
    """An unregistered number messages: if the member told us in advance, finish the move. Returns a reply or None."""
    row = _q("""SELECT * FROM number_changes WHERE new_whatsapp = %s AND status = 'advance' AND created_at > %s
                ORDER BY id DESC LIMIT 1""", (whatsapp_id, _now() - ADVANCE_WINDOW), one=True)
    return _advance_arrived(row) if row else None


def _advance_arrived(row):
    """The new number has just messaged after the member told us in advance. Returns the reply to it."""
    if row["leader_account"]:
        return _to_leaders(row, "They asked for this from their old number first.")
    error = _apply(row, "old_number", tell_new_number=False)
    return error or _welcome(get_member_by_reg_number(row["reg_number"]))


# --- timeouts (every scheduler check) ---------------------------------------------------------------

def check_number_changes():
    now = _now()
    for row in _q("SELECT * FROM number_changes WHERE status = 'awaiting_old' AND created_at < %s", (now - OLD_NUMBER_WINDOW,)):
        if row["old_whatsapp"]:
            pending = get_pending_action(row["old_whatsapp"])
            if pending and pending["action"] == "confirm_number_change":
                clear_pending_action(row["old_whatsapp"])
        _send(row["new_whatsapp"], _to_leaders(row, "Their old number didn't answer within 24 hours."))
    for row in _q("SELECT * FROM number_changes WHERE status = 'awaiting_leader' AND updated_at < %s", (now - LEADER_WINDOW,)):
        _update(row["id"], status="expired")
        _send(row["new_whatsapp"], "Your request to move a DeKUTCU account to this number has expired without "
                                   "being confirmed. Please talk to your group leader.")
    _q("UPDATE number_changes SET status = 'expired', updated_at = %s WHERE status = 'advance' AND created_at < %s",
       (now, now - ADVANCE_WINDOW))
    _q("DELETE FROM number_changes WHERE status = 'asking' AND created_at < %s", (now - ASK_WINDOW,))
