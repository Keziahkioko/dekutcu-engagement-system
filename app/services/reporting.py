"""
app/services/reporting.py

Stage 13: Leadership Reporting over WhatsApp -- leaders ask in plain
language ("Who's been missing fellowships?", "Any unclaimed cases?") and
the model answers from a set of data lookups, the same role-scoped
tool-calling pattern as group_query.py. Plus a weekly summary pushed to
exec leaders every Sunday at 8am (Keziah's choice of timing), built
WITHOUT a model -- straight from these same lookups -- so it can't
misstate a number.

PRIVACY IS ENFORCED IN THE LOOKUPS THEMSELVES, not by instructing the
model: each one only ever returns what the agreed rules allow, so the
model can't reveal something it never received (see PROJECT_LOG.md,
Stage 13 design). The principle: reports help leaders ACT, but members'
own words about their struggles, reasons or personal questions never
become report content.
  - Group leaders get their OWN group only; exec leaders (is_leader) get
    the org-wide lookups. Someone can be both.
  - Absence reasons: the CATEGORY, never the member's words.
  - Escalations: counts, status, response times; a member's name only
    on cases the ASKING leader was notified on; their words never.
  - Feedback: text with no names -- and replies flagged as distress are
    counted, never shown (those are members' words about struggles).
  - Companion questions: "not covered" general questions anonymously;
    pastoral questions counted only.
Uses the default (classifier) model: leaders' report questions are rare
next to members' messages.
"""

import json
from datetime import date, timedelta

from app.database import get_connection
from app.models.member import get_all_leaders, get_member_by_reg_number
from app.models.absence import weekly_streak
from app.models.withdrawal import REMOVED_TEXT, count_withdrawals
from app.services.llm_client import create_chat_completion
from app.services.message_generator import display_name_for
from app.services.whatsapp_client import send_whatsapp_message
from app.services.group_query import finalise_reply

_MAX_TOOL_ROUNDS = 4
_LAPSING_STREAK = 2          # consecutive misses that count as "lapsing"
_LAPSING_LOOKBACK_DAYS = 21  # only CURRENT streaks -- latest miss within 3 weeks
_UTC_NOW = "(NOW() AT TIME ZONE 'UTC')"


def _q(sql, params=()):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def _days(days):
    try:
        return max(1, min(365, int(days)))
    except (TypeError, ValueError):
        return 30


def _label(category):
    """'personal_difficulty' -> 'personal difficulty' -- stored keys stay machine-friendly, reports read naturally."""
    return category.replace("_", " ")


def _pct(part, whole):
    return None if not whole else round(100 * part / whole)


# ---------------------------------------------------------------------
# The lookups. Each returns a small, privacy-safe dict.
# ---------------------------------------------------------------------

def attendance_summary(days=30, group_label=None):
    days = _days(days)
    where, params = "activity_date >= CURRENT_DATE - %s", [days]
    if group_label:
        where += " AND group_label = %s"
        params.append(group_label)
    markings = _q(f"""
        SELECT group_label, COUNT(*) AS sessions_marked,
               SUM(roster_size) AS roster_total, SUM(absent_count) AS absent_total
        FROM attendance_markings WHERE {where} GROUP BY group_label ORDER BY group_label
    """, params)
    bible_study = [{
        "group": m["group_label"],
        "sessions_marked": m["sessions_marked"],
        "attendance_rate_percent": _pct(m["roster_total"] - m["absent_total"], m["roster_total"]),
    } for m in markings]
    result = {"period_days": days, "bible_study": bible_study}
    if group_label:
        return result

    unmarked = _q("""
        SELECT DISTINCT group_label FROM members
        WHERE group_label IS NOT NULL AND group_label NOT IN (
            SELECT group_label FROM attendance_markings WHERE activity_date >= CURRENT_DATE - %s)
        ORDER BY group_label
    """, (days,))
    result["groups_with_no_marking"] = [r["group_label"] for r in unmarked]

    fellowships = _q("""
        SELECT b.activity_type, COUNT(DISTINCT b.checkin_date) AS sessions,
               (SELECT COUNT(*) FROM fellowship_checkins c WHERE c.activity_type = b.activity_type
                  AND c.checkin_date >= CURRENT_DATE - %s) AS said_present,
               (SELECT COUNT(*) FROM absences a WHERE a.activity_type = b.activity_type
                  AND a.activity_date >= CURRENT_DATE - %s) AS absences
        FROM checkin_broadcasts b WHERE b.checkin_date >= CURRENT_DATE - %s
        GROUP BY b.activity_type
    """, (days, days, days))
    result["fellowships"] = [{
        "activity": display_name_for(f["activity_type"]),
        "check_ins_sent_on_days": f["sessions"],
        "members_who_said_present": f["said_present"],
        "absences_recorded": f["absences"],
    } for f in fellowships]
    return result


def lapsing_members(group_label=None):
    """
    Members on a CURRENT streak of missed sessions -- names (follow-up needs them) and the reason CATEGORY only.

    Three queries in total, whatever the amount of history (it used to be one query per member per
    activity -- 291 seconds on a simulated semester). Streaks use the same rule as the bandit
    (absence.weekly_streak). A streak only counts if the member hasn't come back since:
      - Bible Study: their group was marked on a later date and they weren't on that date's absent list;
      - fellowships: they later replied that they attended.
    Silence is NOT coming back -- the system never guesses attendance.
    """
    where, params = "", [_LAPSING_LOOKBACK_DAYS]
    if group_label:
        where = "AND m.group_label = %s"
        params.append(group_label)
    rows = _q(f"""
        SELECT a.reg_number, a.activity_type, a.activity_date, a.reason_category, m.name, m.group_label
        FROM absences a JOIN members m ON m.reg_number = a.reg_number
        WHERE (a.reg_number, a.activity_type) IN (
                  SELECT reg_number, activity_type FROM absences WHERE activity_date >= CURRENT_DATE - %s)
              {where}
        ORDER BY a.reg_number, a.activity_type, a.activity_date DESC
    """, params)
    last_marked = {r["group_label"]: r["last"] for r in _q(
        "SELECT group_label, MAX(activity_date) AS last FROM attendance_markings GROUP BY group_label")}
    last_present = {(r["reg_number"], r["activity_type"]): r["last"] for r in _q(
        "SELECT reg_number, activity_type, MAX(checkin_date) AS last FROM fellowship_checkins GROUP BY 1, 2")}

    by_pair = {}
    for r in rows:
        by_pair.setdefault((r["reg_number"], r["activity_type"]), []).append(r)

    lapsing = []
    for (reg_number, activity_type), absences in by_pair.items():
        latest = absences[0]
        if activity_type == "bible_study":
            came_back = (last_marked.get(latest["group_label"]) or date.min) > latest["activity_date"]
        else:
            came_back = (last_present.get((reg_number, activity_type)) or date.min) > latest["activity_date"]
        if came_back:
            continue
        streak = weekly_streak([a["activity_date"] for a in absences])
        if streak >= _LAPSING_STREAK:
            lapsing.append({
                "name": latest["name"],
                "group": latest["group_label"],
                "activity": display_name_for(activity_type),
                "missed_in_a_row": streak,
                "latest_reason_category": _label(latest["reason_category"]) if latest["reason_category"] else "not given",
            })
    return {"members": sorted(lapsing, key=lambda r: -r["missed_in_a_row"])}


def absence_reasons(days=30):
    rows = _q("""
        SELECT COALESCE(reason_category, 'not given') AS category, COUNT(*) AS n
        FROM absences WHERE activity_date >= CURRENT_DATE - %s
        GROUP BY 1 ORDER BY n DESC
    """, (_days(days),))
    return {"period_days": _days(days), "reason_categories": {_label(r["category"]): r["n"] for r in rows}}


def escalation_summary(asker_reg_number, days=30):
    days = _days(days)
    cases = _q(f"""
        SELECT c.*, EXTRACT(EPOCH FROM (c.claimed_at - c.created_at)) / 60 AS minutes_to_claim,
               EXTRACT(EPOCH FROM ({_UTC_NOW} - c.created_at)) / 60 AS age_minutes,
               EXISTS (SELECT 1 FROM escalations e WHERE e.case_id = c.id
                       AND e.notified_leader_reg_number = %s) AS asker_notified
        FROM escalation_cases c WHERE c.created_at >= {_UTC_NOW} - (%s * INTERVAL '1 day')
        ORDER BY c.created_at
    """, (asker_reg_number, days))
    claimed = [c for c in cases if c["claimed_by_reg_number"]]
    times = sorted(float(c["minutes_to_claim"]) for c in claimed if c["minutes_to_claim"] is not None)
    median = round(times[len(times) // 2]) if times else None
    open_cases = []
    for c in cases:
        if c["claimed_by_reg_number"] or c["closed_at"]:
            continue
        entry = {"case": c["id"], "urgency": c["urgency"], "hours_waiting": round(float(c["age_minutes"]) / 60, 1),
                 "reminder_sent": c["reminded_at"] is not None, "escalated_to_chair": c["backstop_at"] is not None}
        if c["asker_notified"]:
            member = get_member_by_reg_number(c["reg_number"])
            entry["member"] = member["name"] if member else None
        open_cases.append(entry)
    return {
        "period_days": days,
        "total_cases": len(cases),
        "acute_cases": sum(c["urgency"] == "acute" for c in cases),
        "claimed": len(claimed),
        "median_minutes_to_claim": median,
        "needed_a_reminder": sum(c["reminded_at"] is not None for c in cases),
        "escalated_to_chair_or_vice_chairs": sum(c["backstop_at"] is not None for c in cases),
        "closed_because_member_withdrew": sum(c["closed_at"] is not None for c in cases),
        "open_unclaimed_cases": open_cases,
        "note": "Member names appear only on cases you were notified on. Members' own words are never included.",
    }


def feedback_summary(days=30, theme=None):
    days = _days(days)
    theme = theme if theme in ("feedback", "question", "recommendation", "challenge") else None
    by_channel = _q(f"""
        SELECT trigger, COUNT(*) AS sent, COUNT(responded_at) AS responded
        FROM feedback_requests
        WHERE trigger <> 'unprompted' AND sent_at >= {_UTC_NOW} - (%s * INTERVAL '1 day')
        GROUP BY trigger
    """, (days,))
    recent = _q(f"""
        SELECT activity_type, activity_date, response_text, theme FROM feedback_requests
        WHERE response_text IS NOT NULL AND COALESCE(severity, 'none') = 'none'
          AND responded_at >= {_UTC_NOW} - (%s * INTERVAL '1 day')
          AND (%s::text IS NULL OR theme = %s)
        ORDER BY responded_at DESC LIMIT 10
    """, (days, theme, theme))
    themes = _q(f"""
        SELECT COALESCE(theme, 'not yet sorted') AS theme, COUNT(*) AS n FROM feedback_requests
        WHERE response_text IS NOT NULL AND COALESCE(severity, 'none') = 'none'
          AND responded_at >= {_UTC_NOW} - (%s * INTERVAL '1 day')
        GROUP BY 1
    """, (days,))
    counts = _q(f"""
        SELECT COUNT(*) FILTER (WHERE trigger = 'unprompted') AS unprompted,
               COUNT(*) FILTER (WHERE severity IN ('distress', 'acute_risk')) AS flagged
        FROM feedback_requests WHERE responded_at >= {_UTC_NOW} - (%s * INTERVAL '1 day')
    """, (days,))[0]
    questions = _q(f"""
        SELECT id, question, answered_at IS NOT NULL AS answered,
               EXTRACT(EPOCH FROM ({_UTC_NOW} - created_at)) / 86400 AS days_waiting
        FROM member_questions WHERE created_at >= {_UTC_NOW} - (%s * INTERVAL '1 day') AND question <> %s
        ORDER BY created_at
    """, (days, REMOVED_TEXT))
    sent = sum(r["sent"] for r in by_channel)
    responded = sum(r["responded"] for r in by_channel)
    return {
        "period_days": days,
        "overall_response_rate_percent": _pct(responded, sent),
        "by_channel": [{"channel": r["trigger"], "sent": r["sent"], "responded": r["responded"],
                        "response_rate_percent": _pct(r["responded"], r["sent"])} for r in by_channel],
        "unprompted_feedback_count": counts["unprompted"],
        "flagged_as_distress_count": counts["flagged"],
        "themes": {r["theme"]: r["n"] for r in themes},
        "questions_relayed_to_leaders": {
            "total": len(questions),
            "answered": sum(q["answered"] for q in questions),
            "awaiting_answer_anonymous": [
                {"number": q["id"], "question": q["question"], "days_waiting": round(float(q["days_waiting"]), 1)}
                for q in questions if not q["answered"]],
            "how_to_answer": "Reply ANSWER <number> followed by the answer; it's sent to the member, who stays anonymous.",
        },
        "showing_theme": theme or "all",
        "recent_feedback_anonymous": [{"activity": display_name_for(r["activity_type"]), "date": str(r["activity_date"]),
                                       "theme": r["theme"] or "not yet sorted", "text": r["response_text"]} for r in recent],
        "note": "Feedback is anonymous; replies flagged as distress are counted but never shown.",
    }


def companion_questions(days=30):
    days = _days(days)
    counts = _q(f"""
        SELECT kind, outcome, COUNT(*) AS n FROM rag_queries
        WHERE created_at >= {_UTC_NOW} - (%s * INTERVAL '1 day') GROUP BY kind, outcome
    """, (days,))
    uncovered = _q(f"""
        SELECT question, COUNT(*) AS times FROM rag_queries
        WHERE kind = 'general' AND outcome = 'not_covered' AND question <> %s
          AND created_at >= {_UTC_NOW} - (%s * INTERVAL '1 day')
        GROUP BY question ORDER BY times DESC LIMIT 10
    """, (REMOVED_TEXT, days))
    return {
        "period_days": days,
        "counts": [{"kind": r["kind"], "outcome": r["outcome"], "n": r["n"]} for r in counts],
        "not_covered_general_questions_anonymous": [{"question": r["question"], "times_asked": r["times"]} for r in uncovered],
        "note": "Pastoral (personal) questions are counted only, never shown.",
    }


def membership_summary(days=30):
    days = _days(days)
    r = _q(f"""
        SELECT COUNT(*) FILTER (WHERE data_consent) AS registered,
               COUNT(*) FILTER (WHERE data_consent AND registered_at >= {_UTC_NOW} - (%s * INTERVAL '1 day')) AS new_in_period,
               COUNT(*) FILTER (WHERE data_consent AND group_label IS NOT NULL) AS placed_in_group,
               COUNT(*) FILTER (WHERE data_consent AND NOT followup_consent) AS opted_out_of_check_ins,
               COUNT(*) FILTER (WHERE NOT data_consent) AS withdrew_consent,
               COUNT(*) FILTER (WHERE is_leader) AS leaders
        FROM members
    """, (days,))[0]
    result = {"period_days": days, **{k: r[k] for k in r.keys()}}
    # Completed withdrawals leave no member record behind, only an identity-free count (see withdrawal.py);
    # the members-table count above covers anyone whose withdrawal is still waiting on an acute case.
    result["withdrew_consent"] += count_withdrawals()
    return result


def event_rsvps():
    rows = _q("""
        SELECT e.title, e.event_date, e.event_type,
               COUNT(*) FILTER (WHERE r.response = 'yes') AS yes,
               COUNT(*) FILTER (WHERE r.response = 'maybe') AS maybe,
               COUNT(*) FILTER (WHERE r.response = 'no') AS no
        FROM events e LEFT JOIN event_rsvps r ON r.event_id = e.id
        WHERE e.event_date >= CURRENT_DATE GROUP BY e.id ORDER BY e.event_date
    """)
    return {"upcoming_events": [{"title": r["title"], "date": str(r["event_date"]), "type": r["event_type"],
                                 "yes": r["yes"], "maybe": r["maybe"], "no": r["no"]} for r in rows]}


def evaluation_summary():
    """Objective 3: bandit choices and outcomes, adaptive vs the static-reminder control group."""
    arms = _q("""
        SELECT chosen_arm, COUNT(*) AS n, COUNT(reward) AS with_outcome, COUNT(*) FILTER (WHERE reward) AS returned
        FROM absences WHERE chosen_arm IS NOT NULL AND NOT COALESCE(is_control, FALSE)
        GROUP BY chosen_arm ORDER BY n DESC
    """)
    groups = _q("""
        SELECT COALESCE(is_control, FALSE) AS control, COUNT(*) AS n,
               COUNT(recovered_within_2) AS measured, COUNT(*) FILTER (WHERE recovered_within_2) AS recovered
        FROM absences WHERE chosen_arm IS NOT NULL GROUP BY 1
    """)
    return {
        "adaptive_arm_choices": [{"arm": a["chosen_arm"], "times_chosen": a["n"], "outcomes_known": a["with_outcome"],
                                  "returned_next_session_percent": _pct(a["returned"], a["with_outcome"])} for a in arms],
        "recovery_within_2_sessions": [{"group": "control (static reminder)" if g["control"] else "adaptive (bandit)",
                                        "follow_ups": g["n"], "measured": g["measured"],
                                        "recovered_percent": _pct(g["recovered"], g["measured"])} for g in groups],
        "note": "Small numbers early in deployment -- read as descriptive, not statistically significant.",
    }


# ---------------------------------------------------------------------
# Tools offered to the model -- scoped by the ASKER's own role, never by
# anything the model requests.
# ---------------------------------------------------------------------

def _tool(name, description, props=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}}}}


_DAYS_PROP = {"days": {"type": "integer", "description": "How many days back to look (default 30)."}}


def _build_tools(member):
    tools, dispatch = [], {}
    if member.get("leads_group_label"):
        group = member["leads_group_label"]
        tools.append(_tool("my_group_report", f"Attendance and lapsing members for the group you lead ({group}).", _DAYS_PROP))
        dispatch["my_group_report"] = lambda days=30, **kw: {
            "attendance": attendance_summary(days, group_label=group), "lapsing": lapsing_members(group_label=group)}
    if member.get("is_leader"):
        tools += [
            _tool("attendance_summary", "Org-wide Bible Study attendance per group, groups with no marking, and fellowship check-ins.", _DAYS_PROP),
            _tool("lapsing_members", "Members currently missing sessions repeatedly (2+ in a row), with the reason category."),
            _tool("absence_reasons", "Counts of why members missed sessions, by category.", _DAYS_PROP),
            _tool("escalation_summary", "Escalation cases: totals, claimed/unclaimed, response times, open cases.", _DAYS_PROP),
            _tool("feedback_summary", "Feedback response rates per channel, counts per theme, and recent anonymous feedback "
                  "(optionally only one theme).", {**_DAYS_PROP, "theme": {
                      "type": "string", "enum": ["feedback", "question", "recommendation", "challenge"],
                      "description": "Only show feedback of this theme, e.g. 'recommendation' for suggestions members made."}}),
            _tool("companion_questions", "What members asked the RAG companion, especially questions the materials didn't cover.", _DAYS_PROP),
            _tool("membership_summary", "Registrations, new members, group placement, opt-outs, leaders.", _DAYS_PROP),
            _tool("event_rsvps", "Upcoming events with RSVP counts."),
            _tool("evaluation_summary", "Objective 3 evaluation: bandit strategy choices and recovery rates vs the control group."),
        ]
        dispatch.update({
            "attendance_summary": lambda days=30, **kw: attendance_summary(days),
            "lapsing_members": lambda **kw: lapsing_members(),
            "absence_reasons": lambda days=30, **kw: absence_reasons(days),
            "escalation_summary": lambda days=30, **kw: escalation_summary(member["reg_number"], days),
            "feedback_summary": lambda days=30, theme=None, **kw: feedback_summary(days, theme),
            "companion_questions": lambda days=30, **kw: companion_questions(days),
            "membership_summary": lambda days=30, **kw: membership_summary(days),
            "event_rsvps": lambda **kw: event_rsvps(),
            "evaluation_summary": lambda **kw: evaluation_summary(),
        })
    return tools, dispatch


_SYSTEM_PROMPT = (
    "You answer a DeKUTCU leader's question about how the Christian Union is doing, using ONLY the "
    "report tools provided -- call the one(s) that fit, then answer briefly for WhatsApp (plain lines, "
    "*single asterisks* for bold, no tables or headings). Report only what the tools return: never "
    "invent numbers, names or trends, and never add names the tools didn't give you. If a figure is "
    "missing or zero, say so plainly rather than guessing why. Mention small numbers honestly (e.g. "
    "'only 3 sessions marked so far'). If the question isn't covered by any tool, say what you CAN "
    "report on."
)


def answer_leadership_question(member, question):
    tools, dispatch = _build_tools(member)
    if not tools:
        return "Reports are only available to leaders."
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": question}]
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            response = create_chat_completion(messages=messages, tools=tools, tool_choice="auto", temperature=0)
            reply = response.choices[0].message
            if not reply.tool_calls:
                return finalise_reply(reply.content or "I couldn't put that report together -- try asking a bit differently.")
            messages.append({"role": "assistant", "content": reply.content, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in reply.tool_calls]})
            for call in reply.tool_calls:
                func = dispatch.get(call.function.name)
                result = func(**json.loads(call.function.arguments or "{}")) if func else {"error": "unknown report"}
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)})
        return "That report needed more steps than I can take -- try asking about one thing at a time."
    except Exception as e:
        try:
            print(f"Leadership report failed: {e}")
        except UnicodeEncodeError:
            print("Leadership report failed (error message omitted -- contained non-ASCII characters)")
        return "Sorry, I couldn't put that report together right now -- please try again."


# ---------------------------------------------------------------------
# The weekly summary -- Sundays 8am to every exec leader. Built directly
# from the lookups (no model), so every number is exactly the data.
# ---------------------------------------------------------------------

def build_weekly_digest():
    att = attendance_summary(7)
    lapsing = lapsing_members()["members"]
    esc = escalation_summary(asker_reg_number=None, days=7)
    fb = feedback_summary(7)
    cq = companion_questions(7)

    lines = ["*DeKUTCU weekly summary* (last 7 days)", ""]
    bs = att["bible_study"]
    if bs:
        rates = [g["attendance_rate_percent"] for g in bs if g["attendance_rate_percent"] is not None]
        avg = f"{round(sum(rates) / len(rates))}% average attendance" if rates else "no attendance figures"
        lines.append(f"Bible Study: {len(bs)} group(s) marked attendance, {avg}.")
    else:
        lines.append("Bible Study: no attendance was marked this week.")
    if att["groups_with_no_marking"]:
        lines.append(f"Groups with no marking: {len(att['groups_with_no_marking'])}.")
    for f in att["fellowships"]:
        lines.append(f"{f['activity']}: {f['members_who_said_present']} said they were present.")
    lines.append(f"Lapsing members (2+ missed in a row): {len(lapsing)}.")
    open_cases = esc["open_unclaimed_cases"]
    if open_cases:
        lines.append(f"*Unclaimed escalation cases: {len(open_cases)}* -- please check your messages.")
    else:
        lines.append(f"Escalations this week: {esc['total_cases']}, all claimed.")
    rate = fb["overall_response_rate_percent"]
    lines.append(f"Feedback response rate: {rate}%." if rate is not None else "Feedback: no requests sent this week.")
    sorted_themes = {t: n for t, n in fb["themes"].items() if t != "not yet sorted"}
    if sorted_themes:
        order = ["feedback", "question", "recommendation", "challenge"]
        parts = [f"{sorted_themes[t]} {'comment' if t == 'feedback' else t}{'s' if sorted_themes[t] != 1 else ''}"
                 for t in order if t in sorted_themes]
        lines.append("Feedback: " + ", ".join(parts) + ".")
    waiting = feedback_summary(365)["questions_relayed_to_leaders"]["awaiting_answer_anonymous"]
    if waiting:
        lines.append(f"*Members' questions awaiting an answer: {len(waiting)}*")
        lines += [f"  #{w['number']}: \"{w['question']}\"" for w in waiting[:5]]
        lines.append("  Reply ANSWER <number> followed by your answer.")
    uncovered = cq["not_covered_general_questions_anonymous"]
    if uncovered:
        lines.append(f"Questions the materials didn't cover: {len(uncovered)} -- worth adding material on.")
    lines += ["", "Ask me for details, e.g. \"who's been missing?\" or \"show this week's feedback\" -- "
              "or ask for the reports website to see the charts."]
    return "\n".join(lines)


def send_weekly_digest():
    """Scheduled Sundays 8am (registered in app/__init__.py)."""
    digest = build_weekly_digest()
    for leader in get_all_leaders():
        if leader["whatsapp_id"]:
            send_whatsapp_message(leader["whatsapp_id"], digest)
