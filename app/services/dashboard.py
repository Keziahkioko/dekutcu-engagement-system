"""
app/services/dashboard.py

Stage 13 step 4: the reports website -- issuing WhatsApp login links,
and the week-by-week trend data behind its charts. The pages themselves
live in app/routes/dashboard.py.

Everything here is an AGGREGATE (counts and rates per week) -- names
only ever come from the same privacy-scoped lookups the WhatsApp reports
use (reporting.py), so the website can't show anything the agreed
privacy rules don't allow.
"""

import os
import secrets
from datetime import datetime, timezone, timedelta

from app.database import get_connection
from app.models.dashboard_login import store_code
from app.services.message_generator import display_name_for

LINK_LIFETIME = timedelta(minutes=15)
SESSION_LIFETIME = timedelta(hours=12)   # Keziah's choice -- see PROJECT_LOG.md
PERIODS = {7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", 120: "This semester (~4 months)"}


def is_enabled():
    """No secret key, no website -- never fall back to an insecure default key."""
    return bool(os.getenv("DASHBOARD_SECRET_KEY", "").strip())


def can_view(member):
    return bool(member) and (bool(member["is_leader"]) or bool(member["leads_group_label"]))


def _base_url():
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()   # set by Render automatically
    if host:
        return f"https://{host}"
    return os.getenv("DASHBOARD_BASE_URL", "http://localhost:5000").rstrip("/")


def link_reply(member):
    """The WhatsApp reply to a leader asking for the reports website."""
    if not is_enabled():
        return "The reports website isn't switched on yet -- you can still ask me for any report here."
    code = secrets.token_urlsafe(30)   # ~40 characters of randomness
    now = datetime.now(timezone.utc)
    store_code(code, member["reg_number"], now.isoformat(), (now + LINK_LIFETIME).isoformat())
    return (
        "Here's your link to the reports website -- it works once and expires in 15 minutes:\n\n"
        f"{_base_url()}/dashboard/login/{code}\n\n"
        "Once you're in, you'll stay logged in for 12 hours on that browser. Please don't forward it."
    )


def _q(sql, params=()):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def bible_study_weekly(days, group_label=None):
    """Attendance rate per week (from recorded markings), optionally for one group."""
    where, params = "activity_date >= CURRENT_DATE - %s", [days]
    if group_label:
        where += " AND group_label = %s"
        params.append(group_label)
    rows = _q(f"""
        SELECT date_trunc('week', activity_date)::date AS week,
               SUM(roster_size) AS roster, SUM(absent_count) AS absent, COUNT(*) AS markings
        FROM attendance_markings WHERE {where} GROUP BY 1 ORDER BY 1
    """, params)
    return [{"week": str(r["week"]), "markings": r["markings"],
             "rate": round(100 * (r["roster"] - r["absent"]) / r["roster"]) if r["roster"] else None} for r in rows]


def fellowship_weekly(days):
    """
    Members who said they were present, per week per fellowship. A week
    where that fellowship's check-in was never sent (the day hasn't come
    yet this week, or nothing went out) is None -- a GAP in the chart,
    not a 0 -- so the current week doesn't look like a collapse. 0 means
    a check-in went out and nobody said they attended.
    """
    rows = _q("""
        SELECT date_trunc('week', b.checkin_date)::date AS week, b.activity_type,
               COUNT(c.id) AS present
        FROM checkin_broadcasts b
        LEFT JOIN fellowship_checkins c ON c.activity_type = b.activity_type AND c.checkin_date = b.checkin_date
        WHERE b.checkin_date >= CURRENT_DATE - %s
        GROUP BY 1, 2 ORDER BY 1
    """, (days,))
    weeks = sorted({str(r["week"]) for r in rows})
    series = {}
    for r in rows:
        series.setdefault(display_name_for(r["activity_type"]), {})[str(r["week"])] = r["present"]
    return {"weeks": weeks, "series": {name: [by_week.get(w) for w in weeks] for name, by_week in series.items()}}


def _weekly(rows, key_field, value_field="n"):
    """[{week, <key>, n}] -> (weeks, {key: [n per week]}) -- one series per key, zero-filled."""
    weeks = sorted({str(r["week"]) for r in rows})
    series = {}
    for r in rows:
        series.setdefault(str(r[key_field]), {})[str(r["week"])] = r[value_field]
    return {"weeks": weeks, "series": {k: [v.get(w, 0) for w in weeks] for k, v in sorted(series.items())}}


def escalations_weekly(days):
    """Cases per week by urgency, plus the median minutes to claim per week."""
    counts = _q("""
        SELECT date_trunc('week', created_at)::date AS week, urgency, COUNT(*) AS n
        FROM escalation_cases WHERE created_at >= (NOW() AT TIME ZONE 'UTC') - (%s * INTERVAL '1 day')
        GROUP BY 1, 2 ORDER BY 1
    """, (days,))
    medians = _q("""
        SELECT date_trunc('week', created_at)::date AS week,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (claimed_at - created_at)) / 60) AS median
        FROM escalation_cases
        WHERE claimed_at IS NOT NULL AND created_at >= (NOW() AT TIME ZONE 'UTC') - (%s * INTERVAL '1 day')
        GROUP BY 1 ORDER BY 1
    """, (days,))
    result = _weekly(counts, "urgency")
    by_week = {str(m["week"]): round(float(m["median"])) for m in medians}
    result["median_minutes_to_claim"] = [by_week.get(w) for w in result["weeks"]]
    return result


def feedback_weekly(days):
    """Feedback response rate per week (unprompted feedback excluded -- nobody asked, so it isn't a response)."""
    rows = _q("""
        SELECT date_trunc('week', sent_at)::date AS week, COUNT(*) AS sent, COUNT(responded_at) AS responded
        FROM feedback_requests
        WHERE trigger <> 'unprompted' AND sent_at >= (NOW() AT TIME ZONE 'UTC') - (%s * INTERVAL '1 day')
        GROUP BY 1 ORDER BY 1
    """, (days,))
    return [{"week": str(r["week"]), "sent": r["sent"], "responded": r["responded"],
             "rate": round(100 * r["responded"] / r["sent"]) if r["sent"] else None} for r in rows]


def companion_weekly(days):
    """RAG companion questions per week by outcome (answered / not covered / secondary issue / escalated / error)."""
    rows = _q("""
        SELECT date_trunc('week', created_at)::date AS week, outcome, COUNT(*) AS n
        FROM rag_queries WHERE created_at >= (NOW() AT TIME ZONE 'UTC') - (%s * INTERVAL '1 day')
        GROUP BY 1, 2 ORDER BY 1
    """, (days,))
    return _weekly(rows, "outcome")


def registrations_weekly(days):
    rows = _q("""
        SELECT date_trunc('week', registered_at)::date AS week, COUNT(*) AS n
        FROM members WHERE data_consent AND registered_at >= (NOW() AT TIME ZONE 'UTC') - (%s * INTERVAL '1 day')
        GROUP BY 1 ORDER BY 1
    """, (days,))
    return [{"week": str(r["week"]), "n": r["n"]} for r in rows]


def arm_share_weekly(days):
    """
    Objective 3 -- policy convergence: which strategy the bandit picked each
    week (adaptive members only; the control group always gets "reminder").
    A policy that's learning shifts from roughly even shares toward the
    strategies that work.
    """
    rows = _q("""
        SELECT date_trunc('week', activity_date)::date AS week, chosen_arm, COUNT(*) AS n
        FROM absences
        WHERE chosen_arm IS NOT NULL AND NOT COALESCE(is_control, FALSE) AND activity_date >= CURRENT_DATE - %s
        GROUP BY 1, 2 ORDER BY 1
    """, (days,))
    return _weekly(rows, "chosen_arm")
