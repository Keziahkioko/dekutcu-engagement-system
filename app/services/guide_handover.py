"""
app/services/guide_handover.py

Stage 14 step 4, part 4: handing printed guides to members, and the stock
view (design with Keziah 2026-10-05 -- see PROJECT_LOG.md).

Hand-over (Keziah's choice over a collection code):
  1. A group leader says "hand over guides" -> a numbered list of their
     group's members who've paid and not collected (older guides labelled);
     the Guides Coordinator's list is members with NO group leader. They
     reply with the numbers ("2, 5", "2 and 5").
  2. Those purchases are marked handed over by that leader -- their stock goes
     down now, because the copy has physically gone.
  3. The member is asked "did you receive your copy from Jane? YES/NO".
       YES -> confirmed.
       NO  -> the hand-over is reversed (stock back, member back on the
              waiting list) and the leader and the Coordinator are told.
       no reply -> stays UNCONFIRMED indefinitely, visible in the stock view
              (Keziah's choice).
     NOT blocking (agreed with Keziah): an unrelated message is answered
     normally with a one-time reminder line; YES/NO counts whenever it comes.
     A blocked member's crisis message would never reach the safety check.

Stock view ("guide stock"): for the Coordinator, the Director and exec
leaders -- per group leader: confirmed copies received, handed over, in hand,
paid-and-waiting, unconfirmed batches and unconfirmed hand-overs; plus members
with no group leader waiting on the Coordinator.
"""

import re
from datetime import datetime, timezone

from app.database import get_connection
from app.models.member import get_member_by_whatsapp_id, get_member_by_reg_number
from app.models.study_guide import get_current_guide, copies_in_hand
from app.services import conversation, guide_coordinator
from app.services.whatsapp_client import send_whatsapp_message


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


def init_handover_tables():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_handover (
            whatsapp_id TEXT PRIMARY KEY,
            purchase_ids TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


# --- who's waiting on whom -----------------------------------------------------------------------

def _waiting_for(member):
    """
    Paid, uncollected purchases this person hands out: their own group's (if a group leader) plus,
    for the Guides Coordinator, members who have no group leader. Oldest guide first.
    """
    clauses, params = [], []
    if member["leads_group_label"]:
        clauses.append("m.group_label = %s")
        params.append(member["leads_group_label"])
    if guide_coordinator.is_coordinator(member):
        clauses.append("""(m.group_label IS NULL OR NOT EXISTS (
                             SELECT 1 FROM members l WHERE l.leads_group_label = m.group_label AND l.data_consent))""")
    if not clauses:
        return []
    return _q(f"""SELECT p.id, p.guide_id, p.reg_number, g.title, g.is_current, m.name, m.whatsapp_id
                  FROM guide_purchases p JOIN members m ON m.reg_number = p.reg_number
                  JOIN study_guides g ON g.id = p.guide_id
                  WHERE p.status = 'paid' AND p.collected_at IS NULL AND ({' OR '.join(clauses)})
                  ORDER BY g.started_at, m.name""", params)


def can_hand_over(member):
    return bool(member) and (bool(member["leads_group_label"]) or guide_coordinator.is_coordinator(member))


# --- the leader's hand-over ----------------------------------------------------------------------

def begin(member):
    """The hand_over_guides intent."""
    if not can_hand_over(member):
        return "Only group leaders (and the Guides Coordinator) hand out study guides."
    waiting = _waiting_for(member)
    if not waiting:
        return "Nobody is waiting to collect a guide from you right now. 👍"
    _q("""INSERT INTO pending_handover (whatsapp_id, purchase_ids, created_at) VALUES (%s, %s, %s)
          ON CONFLICT (whatsapp_id) DO UPDATE SET purchase_ids = EXCLUDED.purchase_ids, created_at = EXCLUDED.created_at""",
       (member["whatsapp_id"], ",".join(str(p["id"]) for p in waiting), _now()))
    lines = [f"{i + 1}. {p['name']} -- '{p['title']}'{'' if p['is_current'] else ' (older guide)'}"
             for i, p in enumerate(waiting)]
    return ("Who did you give a guide to?\n\n" + "\n".join(lines) +
            "\n\nReply with their numbers (e.g. 2, 5) -- they'll each be asked to confirm. Or 'cancel'.")


def get_pending(whatsapp_id):
    return _q("SELECT * FROM pending_handover WHERE whatsapp_id = %s", (whatsapp_id,), one=True)


def delete_pending(whatsapp_id):
    _q("DELETE FROM pending_handover WHERE whatsapp_id = %s", (whatsapp_id,))


def _parse_numbers(text, size):
    tokens = [t for t in re.split(r"[\s,&]+", conversation.normalise(text)) if t and t != "and"]
    if not tokens or not all(t.isdigit() for t in tokens):
        return None
    numbers = sorted({int(t) for t in tokens})
    return numbers if all(1 <= n <= size for n in numbers) else []


def handle_message(whatsapp_id, message_text):
    """The leader's reply with the numbers. Returns the reply, or None (not an answer -> handled normally)."""
    pending = get_pending(whatsapp_id)
    if pending is None:
        return None
    if (_now() - pending["created_at"]).total_seconds() > 30 * 60:
        delete_pending(whatsapp_id)
        return None
    ids = [int(i) for i in pending["purchase_ids"].split(",") if i]
    numbers = _parse_numbers(message_text, len(ids))
    if numbers is None:
        return None if conversation.looks_like_new_request(message_text) else \
            "Please reply with the numbers of the people you gave a guide to (e.g. 2, 5) -- or 'cancel'."
    if not numbers:
        return f"Please use numbers from 1 to {len(ids)} (or 'cancel')."
    delete_pending(whatsapp_id)
    leader = get_member_by_whatsapp_id(whatsapp_id)
    names, skipped = [], 0
    for n in numbers:
        row = _q("""UPDATE guide_purchases SET collected_at = %s, collected_by_reg_number = %s,
                           receipt_status = 'awaiting', receipt_answered_at = NULL, receipt_reminded = FALSE
                    WHERE id = %s AND status = 'paid' AND collected_at IS NULL RETURNING *""",
                 (_now(), leader["reg_number"], ids[n - 1]), one=True)
        if not row:
            skipped += 1     # someone else recorded it meanwhile
            continue
        member = get_member_by_reg_number(row["reg_number"])
        title = _q("SELECT title FROM study_guides WHERE id = %s", (row["guide_id"],), one=True)["title"]
        names.append(member["name"] if member else "a member")
        if member and member["whatsapp_id"]:
            send_whatsapp_message(member["whatsapp_id"],
                                  f"{leader['name']} has recorded giving you your copy of '{title}'. "
                                  "Did you receive it? Reply YES or NO.")
    reply = f"Recorded -- {', '.join(names)}. They've each been asked to confirm they received it."
    if skipped:
        reply += f" ({skipped} had already been recorded by someone else.)"
    guide = get_current_guide()
    if guide and leader["leads_group_label"]:
        in_hand = copies_in_hand(leader["reg_number"], guide["id"])
        if in_hand < 0:
            reply += (f"\n\nYour records show {in_hand + len(names)} copies before this -- ask the Guides Coordinator "
                      "to record the batch you received, or reply RECEIVED if one is waiting for you.")
        else:
            reply += f" You have {in_hand} {'copy' if in_hand == 1 else 'copies'} of '{guide['title']}' left."
    return reply


# --- the member confirms --------------------------------------------------------------------------

def _awaiting(reg_number):
    return _q("""SELECT p.*, g.title FROM guide_purchases p JOIN study_guides g ON g.id = p.guide_id
                 WHERE p.reg_number = %s AND p.receipt_status = 'awaiting' ORDER BY p.collected_at LIMIT 1""",
              (reg_number,), one=True)


def handle_receipt_answer(whatsapp_id, message_text):
    """A YES/NO from a member who has a hand-over to confirm. Returns the reply, or None if not applicable."""
    member = get_member_by_whatsapp_id(whatsapp_id)
    if not member:
        return None
    answer = conversation.strict_yes_no(message_text)
    if answer is None:
        return None
    purchase = _awaiting(member["reg_number"])
    if not purchase:
        return None
    giver = get_member_by_reg_number(purchase["collected_by_reg_number"])
    giver_name = giver["name"] if giver else "the leader"
    if answer == "yes":
        _q("UPDATE guide_purchases SET receipt_status = 'confirmed', receipt_answered_at = %s WHERE id = %s",
           (_now(), purchase["id"]))
        return f"Great -- enjoy '{purchase['title']}'! 📖"
    _q("""UPDATE guide_purchases SET receipt_status = 'disputed', receipt_answered_at = %s,
              collected_at = NULL, collected_by_reg_number = NULL WHERE id = %s""", (_now(), purchase["id"]))
    notice = f"{member['name']} says they haven't received their copy of '{purchase['title']}' from {giver_name}."
    told = set()
    for person in (giver, guide_coordinator.get_effective_coordinator()):
        if person and person["whatsapp_id"] and person["reg_number"] not in told:
            send_whatsapp_message(person["whatsapp_id"], notice + " It's back on the waiting list.")
            told.add(person["reg_number"])
    return (f"Thanks for letting me know -- I've told {giver_name} and the Guides Coordinator, and you're back on "
            "the list to collect your copy.")


def reminder_for(whatsapp_id):
    """A one-time reminder line to append to an unrelated reply, or ''."""
    member = get_member_by_whatsapp_id(whatsapp_id)
    if not member:
        return ""
    purchase = _q("""SELECT p.id, g.title, p.collected_by_reg_number FROM guide_purchases p
                     JOIN study_guides g ON g.id = p.guide_id
                     WHERE p.reg_number = %s AND p.receipt_status = 'awaiting' AND NOT p.receipt_reminded
                     ORDER BY p.collected_at LIMIT 1""", (member["reg_number"],), one=True)
    if not purchase:
        return ""
    _q("UPDATE guide_purchases SET receipt_reminded = TRUE WHERE id = %s", (purchase["id"],))
    giver = get_member_by_reg_number(purchase["collected_by_reg_number"])
    who = f" from {giver['name']}" if giver else ""
    return f"\n\n(Also: did you receive your copy of '{purchase['title']}'{who}? Reply YES or NO.)"


# --- the stock view ---------------------------------------------------------------------------------

def can_view_stock(member):
    return bool(member) and (guide_coordinator.is_coordinator(member) or bool(member["is_leader"]))


def stock_rows(guide_id):
    """
    Per group leader, for one guide: confirmed copies received, handed out, in hand, paid-and-waiting,
    unconfirmed batches and unconfirmed hand-overs. Shared by "guide stock", the WhatsApp reports and
    the reports website, so they can never disagree. Leaders with nothing to show are left out.
    """
    rows = []
    for l in _q("SELECT * FROM members WHERE leads_group_label IS NOT NULL AND data_consent ORDER BY leads_group_label"):
        received = int(_q("""SELECT COALESCE(SUM(copies_received), 0) AS n FROM guide_batches
                             WHERE leader_reg_number = %s AND guide_id = %s AND status = 'confirmed'""",
                          (l["reg_number"], guide_id), one=True)["n"])
        unconfirmed_batches = _q("""SELECT COUNT(*) AS n FROM guide_batches WHERE leader_reg_number = %s
                                    AND guide_id = %s AND status = 'pending'""", (l["reg_number"], guide_id), one=True)["n"]
        handed = _q("""SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE receipt_status = 'awaiting') AS unconfirmed
                       FROM guide_purchases WHERE collected_by_reg_number = %s AND guide_id = %s
                       AND collected_at IS NOT NULL""", (l["reg_number"], guide_id), one=True)
        waiting = _q("""SELECT COUNT(*) AS n FROM guide_purchases p JOIN members m ON m.reg_number = p.reg_number
                        WHERE m.group_label = %s AND p.guide_id = %s AND p.status = 'paid' AND p.collected_at IS NULL""",
                     (l["leads_group_label"], guide_id), one=True)["n"]
        if not (received or unconfirmed_batches or handed["n"] or waiting):
            continue
        rows.append({"group": l["leads_group_label"], "leader": l["name"], "received": received,
                     "handed_out": handed["n"], "in_hand": received - handed["n"], "waiting": waiting,
                     "needs_more": waiting > received - handed["n"], "unconfirmed_batches": unconfirmed_batches,
                     "unconfirmed_handovers": handed["unconfirmed"]})
    return rows


def waiting_without_leader(guide_id):
    """Paid members of this guide with no group leader -- they collect from the Guides Coordinator."""
    return _q("""SELECT COUNT(*) AS n FROM guide_purchases p JOIN members m ON m.reg_number = p.reg_number
                 WHERE p.guide_id = %s AND p.status = 'paid' AND p.collected_at IS NULL
                 AND (m.group_label IS NULL OR NOT EXISTS (SELECT 1 FROM members l
                      WHERE l.leads_group_label = m.group_label AND l.data_consent))""", (guide_id,), one=True)["n"]


def stock_view(member):
    """The guide_stock intent."""
    if not can_view_stock(member):
        return "Only the Guides Coordinator and exec leaders can see the guide stock."
    guide = get_current_guide()
    if not guide:
        return "There's no study guide on sale right now."
    gid = guide["id"]
    lines, short = [], 0
    for r in stock_rows(gid):
        flags = []
        if r["needs_more"]:
            flags.append("⚠️ needs more")
            short += 1
        if r["unconfirmed_batches"]:
            flags.append(f"{r['unconfirmed_batches']} batch(es) not confirmed")
        if r["unconfirmed_handovers"]:
            flags.append(f"{r['unconfirmed_handovers']} hand-over(s) not confirmed by the member")
        lines.append(f"• {r['group']} ({r['leader']}): received {r['received']}, handed out {r['handed_out']}, "
                     f"in hand {r['in_hand']}, waiting {r['waiting']}" + (f" -- {'; '.join(flags)}" if flags else ""))
    no_leader = waiting_without_leader(gid)
    older = _q("""SELECT COUNT(*) AS n FROM guide_purchases WHERE guide_id <> %s AND status = 'paid'
                  AND collected_at IS NULL""", (gid,), one=True)["n"]
    totals = _q("""SELECT COUNT(*) FILTER (WHERE status = 'paid') AS paid,
                          COUNT(*) FILTER (WHERE status = 'paid' AND collected_at IS NOT NULL) AS collected
                   FROM guide_purchases WHERE guide_id = %s""", (gid,), one=True)
    text = (f"*'{guide['title']}' stock*\nPaid: {totals['paid']} · handed out: {totals['collected']}"
            + (f" · groups needing more: {short}" if short else "") + "\n\n"
            + ("\n".join(lines) if lines else "No batches or payments yet."))
    if no_leader:
        text += f"\n\n• No group leader: {no_leader} paid member(s) collect from the Guides Coordinator."
    if older:
        text += f"\n• Older guides: {older} paid copy/copies still to hand over."
    return text
