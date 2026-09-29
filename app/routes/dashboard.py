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
"""

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


@dashboard_bp.route("/overview")
@_page("exec")
def overview(member):
    days = _days()
    return render_template(
        "dashboard/overview.html", viewer=member, days=days, periods=dashboard.PERIODS, active="overview",
        attendance=reporting.attendance_summary(days),
        lapsing=reporting.lapsing_members()["members"],
        escalations=reporting.escalation_summary(member["reg_number"], days),
        feedback=reporting.feedback_summary(days),
        bible_study_weekly=dashboard.bible_study_weekly(days),
        fellowship_weekly=dashboard.fellowship_weekly(days),
    )


@dashboard_bp.route("/my-group")
@_page("group")
def my_group(member):
    days = _days()
    group = member["leads_group_label"]
    return render_template(
        "dashboard/my_group.html", viewer=member, days=days, periods=dashboard.PERIODS, active="my_group",
        group=group,
        attendance=reporting.attendance_summary(days, group_label=group),
        lapsing=reporting.lapsing_members(group_label=group)["members"],
        bible_study_weekly=dashboard.bible_study_weekly(days, group_label=group),
    )


@dashboard_bp.context_processor
def _navigation():
    """The exec pages beyond Overview, shown in the header for exec leaders."""
    return {"extra_pages": [("Care & feedback", "dashboard.care"),
                            ("Companion & members", "dashboard.community"),
                            ("Evaluation", "dashboard.evaluation")]}


@dashboard_bp.route("/care")
@_page("exec")
def care(member):
    days = _days()
    return render_template(
        "dashboard/care.html", viewer=member, days=days, periods=dashboard.PERIODS, active="care",
        escalations=reporting.escalation_summary(member["reg_number"], days),
        escalations_weekly=dashboard.escalations_weekly(days),
        reasons=reporting.absence_reasons(days)["reason_categories"],
        feedback=reporting.feedback_summary(days),
        feedback_weekly=dashboard.feedback_weekly(days),
    )


@dashboard_bp.route("/community")
@_page("exec")
def community(member):
    days = _days()
    return render_template(
        "dashboard/community.html", viewer=member, days=days, periods=dashboard.PERIODS, active="community",
        companion=reporting.companion_questions(days),
        companion_weekly=dashboard.companion_weekly(days),
        membership=reporting.membership_summary(days),
        registrations_weekly=dashboard.registrations_weekly(days),
        events=reporting.event_rsvps()["upcoming_events"],
    )


@dashboard_bp.route("/evaluation")
@_page("exec")
def evaluation(member):
    days = _days()
    return render_template(
        "dashboard/evaluation.html", viewer=member, days=days, periods=dashboard.PERIODS, active="evaluation",
        evaluation=reporting.evaluation_summary(),
        arm_share_weekly=dashboard.arm_share_weekly(days),
    )
