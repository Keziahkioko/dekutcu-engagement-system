"""
app/routes/dashboard.py

Stage 13 step 4: the reports website (the bot calls it "the reports
website"; "dashboard" is only the internal name). Login design settled
with Keziah -- see PROJECT_LOG.md:
  - a leader asks the bot for the link in any words; they get a
    one-time link (code stored hashed, single-use, 15-minute expiry);
  - opening it starts a SIGNED session cookie (DASHBOARD_SECRET_KEY),
    HTTP-only, HTTPS-only on Render, lasting 12 hours;
  - EVERY page re-checks the viewer's CURRENT role -- losing leader
    access (e.g. an exec office ending) cuts the website off at once,
    whatever is left of the session;
  - a Log out button ends the session immediately (for shared computers).
Exec leaders see the org-wide pages; group leaders see only their own
group. All data comes from the privacy-scoped reporting lookups.

Redesign (Keziah, 2026-10-07): pages follow the question a leader is
asking, and each number appears ONCE --
  Home        "What needs my attention?"  (to-do list + 4 headline numbers)
  Attendance  "Are people coming?"
  Care        "Is anyone struggling, and what are members saying?"
  Community   "How is the CU growing and engaging?"
  Evaluation  "Is the adaptive follow-up working?" (Objective 3)
The Home to-do list shows only counts and points to the page with the detail.
"""

import os
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, render_template, redirect, request, session, url_for, abort

from app.models.dashboard_login import claim_code
from app.models.member import get_member_by_reg_number
from app.services import dashboard, reporting

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


def _viewer():
    """The logged-in member, re-read from the database on EVERY request -- or None."""
    reg_number = session.get("reg_number")
    if not reg_number:
        return None
    member = get_member_by_reg_number(reg_number)
    if not dashboard.can_view(member):
        session.clear()   # lost leader access since logging in
        return None
    return member


def _page(role):
    """role: 'exec' (is_leader) or 'group' (leads a group)."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not dashboard.is_enabled():
                return _message("The reports website isn't switched on yet."), 503
            member = _viewer()
            if member is None:
                return _message("You're not logged in. Ask the bot on WhatsApp for a link to the reports website."), 401
            if role == "exec" and not member["is_leader"]:
                abort(403)
            if role == "group" and not member["leads_group_label"]:
                abort(403)
            return view(member, *args, **kwargs)
        return wrapped
    return decorator


def _message(text):
    return render_template("dashboard/message.html", message=text, viewer=None)


def _days():
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    return days if days in dashboard.PERIODS else 30


def _render(template, member, active, days, **data):
    return render_template(f"dashboard/{template}.html", viewer=member, days=days, periods=dashboard.PERIODS,
                           active=active, **data)


@dashboard_bp.route("/login/<code>")
def login(code):
    if not dashboard.is_enabled():
        return _message("The reports website isn't switched on yet."), 503
    reg_number = claim_code(code, datetime.now(timezone.utc).isoformat())
    member = get_member_by_reg_number(reg_number) if reg_number else None
    if not dashboard.can_view(member):
        return _message("That link has expired or was already used. Ask the bot on WhatsApp for a new one."), 401
    session.clear()
    session.permanent = True   # lasts SESSION_LIFETIME (12h), set on the app in app/__init__.py
    session["reg_number"] = member["reg_number"]
    return redirect(url_for("dashboard.home"))


@dashboard_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return _message("You're logged out. Ask the bot on WhatsApp whenever you need a new link.")


@dashboard_bp.route("/")
def home():
    member = _viewer()
    if member is None:
        return _message("You're not logged in. Ask the bot on WhatsApp for a link to the reports website."), 401
    return redirect(url_for("dashboard.overview" if member["is_leader"] else "dashboard.my_group"))


@dashboard_bp.context_processor
def _navigation():
    """
    The exec pages, shown in the side bar (bottom tabs on a phone) for exec leaders; and demo_mode -- set
    only by demo/run_demo.py (never on Render), which puts a "DEMO DATA" banner on every page so synthetic
    data can't be mistaken for real results.
    """
    return {"nav_pages": [("Home", "dashboard.overview", "home"),
                          ("Attendance", "dashboard.attendance", "attendance"),
                          ("Care", "dashboard.care", "care"),
                          ("Community", "dashboard.community", "community"),
                          ("Evaluation", "dashboard.evaluation", "evaluation")],
            "demo_mode": os.getenv("DEMO_MODE") == "true"}


def _todo(attendance, lapsing, escalations, feedback, guides):
    """The Home page's "needs your attention" list -- counts only; the detail lives on one other page."""
    items = []
    cases = escalations["open_unclaimed_cases"]
    if cases:
        acute = sum(c["urgency"] == "acute" for c in cases)
        items.append({"icon": "alert", "urgent": bool(acute), "page": "dashboard.care",
                      "title": f"{len(cases)} care case{'s' if len(cases) != 1 else ''} waiting for a leader"
                               + (f" — {acute} urgent" if acute else ""),
                      "detail": f"Oldest waiting {max(c['hours_waiting'] for c in cases)} hours. "
                                "Take one by replying CLAIM <case> to the bot."})
    waiting = feedback["questions_relayed_to_leaders"]["awaiting_answer_anonymous"]
    if waiting:
        items.append({"icon": "question", "urgent": False, "page": "dashboard.care",
                      "title": f"{len(waiting)} member question{'s' if len(waiting) != 1 else ''} waiting for an answer",
                      "detail": "Anonymous questions the bot passed to leaders. Reply ANSWER <number> to the bot."})
    if lapsing:
        items.append({"icon": "attendance", "urgent": False, "page": "dashboard.attendance",
                      "title": f"{len(lapsing)} member{'s' if len(lapsing) != 1 else ''} missing repeatedly",
                      "detail": "On a streak of 2 or more missed sessions — a personal word from a leader can help."})
    unmarked = attendance["groups_with_no_marking"]
    if unmarked:
        items.append({"icon": "attendance", "urgent": False, "page": "dashboard.attendance",
                      "title": f"{len(unmarked)} Bible Study group{'s' if len(unmarked) != 1 else ''} with no attendance marked",
                      "detail": ", ".join(unmarked[:4]) + (f" and {len(unmarked) - 4} more" if len(unmarked) > 4 else "")})
    current = next((g for g in guides if g["on_sale"]), None)
    if current and current["refunds_needed"]:
        n = len(current["refunds_needed"])
        items.append({"icon": "book", "urgent": False, "page": "dashboard.community",
                      "title": f"{n} study guide refund{'s' if n != 1 else ''} needed",
                      "detail": "Members who paid twice for the same guide."})
    short = [r for r in (current or {}).get("stock_per_group", []) if r["needs_more"]]
    if short:
        items.append({"icon": "book", "urgent": False, "page": "dashboard.community",
                      "title": f"{len(short)} group{'s need' if len(short) != 1 else ' needs'} more study guide copies",
                      "detail": "More members have paid than copies in hand — the Guides Coordinator can send a batch."})
    return items


@dashboard_bp.route("/overview")
@_page("exec")
def overview(member):
    """Home: what needs attention, then four headline numbers with the week-on-week change."""
    days = _days()
    attendance = reporting.attendance_summary(days)
    lapsing = reporting.lapsing_members()["members"]
    escalations = reporting.escalation_summary(member["reg_number"], days)
    feedback = reporting.feedback_summary(days)
    guides = reporting.study_guide_summary()["study_guides"]
    membership = reporting.membership_summary(days)

    bs_weekly = dashboard.bible_study_weekly(days)
    rates = [g["attendance_rate_percent"] for g in attendance["bible_study"] if g["attendance_rate_percent"] is not None]
    fellowship = dashboard.fellowship_weekly(days)
    fellowship_avg = dashboard.fellowship_average_per_week(fellowship)
    held = [v for v in fellowship_avg if v is not None]
    kpis = [
        {"label": "Bible Study attendance", "icon": "attendance", "unit": "%",
         "value": round(sum(rates) / len(rates)) if rates else None,
         "trend": dashboard.trend([w["rate"] for w in bs_weekly]), "trend_unit": " pts",
         "hint": f"Average across {len(rates)} group{'s' if len(rates) != 1 else ''} that marked attendance"},
        {"label": "At each fellowship", "icon": "smile", "unit": " people",
         "value": round(sum(held) / len(held)) if held else None,
         "trend": dashboard.fellowship_trend(fellowship), "trend_unit": "",
         "hint": "On average, said \"I was there\" to the evening check-in"},
        {"label": "Answer when asked for feedback", "icon": "chat", "unit": "%",
         "value": feedback["overall_response_rate_percent"],
         "trend": dashboard.trend([w["rate"] for w in dashboard.feedback_weekly(days)]), "trend_unit": " pts",
         "hint": "Members who reply to \"how was it?\""},
        {"label": "New members", "icon": "user-plus", "unit": "",
         "value": membership["new_in_period"], "trend": "skip",
         "hint": f"{membership['registered']} registered in total"},
    ]
    return _render("overview", member, "overview", days, kpis=kpis,
                   todo=_todo(attendance, lapsing, escalations, feedback, guides))


@dashboard_bp.route("/attendance")
@_page("exec")
def attendance(member):
    days = _days()
    return _render("attendance", member, "attendance", days,
                   attendance=reporting.attendance_summary(days),
                   lapsing=reporting.lapsing_members()["members"],
                   bible_study_weekly=dashboard.bible_study_weekly(days),
                   fellowship_weekly=dashboard.fellowship_weekly(days))


@dashboard_bp.route("/my-group")
@_page("group")
def my_group(member):
    days = _days()
    group = member["leads_group_label"]
    return _render("my_group", member, "my_group", days, group=group,
                   attendance=reporting.attendance_summary(days, group_label=group),
                   lapsing=reporting.lapsing_members(group_label=group)["members"],
                   bible_study_weekly=dashboard.bible_study_weekly(days, group_label=group),
                   guides=reporting.my_group_guides(group))


@dashboard_bp.route("/care")
@_page("exec")
def care(member):
    days = _days()
    return _render("care", member, "care", days,
                   escalations=reporting.escalation_summary(member["reg_number"], days),
                   escalations_weekly=dashboard.escalations_weekly(days),
                   reasons=reporting.absence_reasons(days)["reason_categories"],
                   feedback=reporting.feedback_summary(days),
                   feedback_weekly=dashboard.feedback_weekly(days))


@dashboard_bp.route("/community")
@_page("exec")
def community(member):
    days = _days()
    return _render("community", member, "community", days,
                   companion=reporting.companion_questions(days),
                   companion_weekly=dashboard.companion_weekly(days),
                   membership=reporting.membership_summary(days),
                   registrations_weekly=dashboard.registrations_weekly(days),
                   events=reporting.event_rsvps()["upcoming_events"],
                   guides=reporting.study_guide_summary()["study_guides"])


@dashboard_bp.route("/evaluation")
@_page("exec")
def evaluation(member):
    days = _days()
    return _render("evaluation", member, "evaluation", days,
                   evaluation=reporting.evaluation_summary(),
                   arm_share_weekly=dashboard.arm_share_weekly(days))
