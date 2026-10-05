"""
app/__init__.py

The app factory. This is the ONE place that builds the Flask app:
loads environment variables, registers routes (blueprints), and
initializes the database tables.

Every other file (run.py, tests) should go through create_app()
instead of building a Flask app on its own.
"""

import os
import functools

from flask import Flask
from dotenv import load_dotenv

from app.routes.webhook import webhook_bp, start_message_worker
from app.routes.dashboard import dashboard_bp
from app.routes.mpesa import mpesa_bp
from app.services.dashboard import SESSION_LIFETIME
from app.models.dashboard_login import init_dashboard_login_table
from app.models.member import init_members_table
from app.models.pending_registration import init_pending_registrations_table
from app.models.pending_action import init_pending_actions_table
from app.models.pending_leader_nomination import init_pending_leader_nominations_table
from app.models.pending_area_change import init_pending_area_changes_table
from app.models.pending_reassignment_resolution import init_pending_reassignment_resolutions_table
from app.models.conversation_history import init_conversation_history_table
from app.models.pending_message import init_pending_messages_table
from app.models.event import init_events_table
from app.models.event_rsvp import init_event_rsvps_table
from app.models.pending_event_creation import init_pending_event_creation_table
from app.models.pending_rsvp import init_pending_rsvps_table
from app.models.scheduled_task_run import init_scheduled_task_runs_table
from app.models.pending_attendance_marking import init_pending_attendance_marking_table
from app.models.absence import init_absences_table
from app.models.pending_reason_capture import init_pending_reason_capture_table
from app.models.fellowship_checkin import init_fellowship_checkins_table
from app.models.pending_fellowship_checkin import init_pending_fellowship_checkin_table
from app.models.bandit_posterior import init_bandit_posteriors_table
from app.models.escalation import init_escalations_table
from app.models.pending_escalation_consent import init_pending_escalation_consent_table
from app.models.checkin_broadcast import init_checkin_broadcasts_table
from app.models.feedback_request import init_feedback_requests_table
from app.models.pending_feedback import init_pending_feedback_table
from app.models.rag import init_rag_tables
from app.models.pending_exec_role import init_pending_exec_role_table
from app.models.attendance_marking import init_attendance_markings_table
from app.models.member_question import init_member_question_tables
from app.models.withdrawal import init_consent_withdrawals_table
from app.models.study_guide import init_study_guide_tables
from app.services.announcements import init_announcement_tables
from app.services.number_change import init_number_change_table, check_number_changes
from app.services.withdrawal import finish_pending_withdrawals
from app.services.guide_payments import check_unsettled_purchases
from app.models.pending_message import forget_old_message_ids
from app.services.reporting import send_weekly_digest
from app.services.feedback_themes import sort_pending_feedback
from app.services.scheduler import start_scheduler, register_task, register_every_check_task
from app.services.escalation import follow_up_unclaimed_cases
from app.services.attendance import send_bible_study_nudges
from app.services.fellowship_checkin import send_fellowship_checkin, process_stale_checkins, TRACKED_WEEKDAYS
from app.services.bandit import compute_pending_rewards


def create_app():
    load_dotenv()  # loads .env before anything else needs those values

    app = Flask(__name__)

    # Register route blueprints
    app.register_blueprint(webhook_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(mpesa_bp)

    # The reports website's login session (Stage 13 -- see
    # app/routes/dashboard.py). Signed with DASHBOARD_SECRET_KEY; with no
    # key the website stays switched off rather than using a weak default.
    # HTTP-only (page scripts can't read it), HTTPS-only on Render, 12 hours.
    app.secret_key = os.getenv("DASHBOARD_SECRET_KEY") or None
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.getenv("RENDER") == "true",
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    )

    # Make sure all tables exist before the app starts serving requests
    init_members_table()
    init_pending_registrations_table()
    init_pending_actions_table()
    init_pending_leader_nominations_table()
    init_pending_area_changes_table()
    init_pending_reassignment_resolutions_table()
    init_conversation_history_table()
    init_pending_messages_table()
    init_events_table()
    init_event_rsvps_table()
    init_pending_event_creation_table()
    init_pending_rsvps_table()
    init_scheduled_task_runs_table()
    init_pending_attendance_marking_table()
    init_absences_table()
    init_pending_reason_capture_table()
    init_fellowship_checkins_table()
    init_pending_fellowship_checkin_table()
    init_bandit_posteriors_table()
    init_escalations_table()
    init_pending_escalation_consent_table()
    init_checkin_broadcasts_table()
    init_feedback_requests_table()
    init_pending_feedback_table()  # after feedback_requests -- it references that table
    init_rag_tables()
    init_pending_exec_role_table()
    init_attendance_markings_table()
    init_member_question_tables()
    init_dashboard_login_table()
    init_consent_withdrawals_table()
    init_study_guide_tables()
    init_announcement_tables()
    init_number_change_table()

    run_workers = _should_run_background_workers()
    if run_workers:
        start_message_worker()
    else:
        print("Background workers NOT started (not on Render) -- set RUN_BACKGROUND_WORKERS=true to override.")

    # Tuesday, 21:00 Nairobi time -- weekday 1 = Tuesday (Monday=0 ... Sunday=6)
    register_task("bible_study_nudge", weekday=1, hour=21, func=send_bible_study_nudges)

    # 21:00 each tracked fellowship day -- functools.partial binds each
    # specific weekday value now, at registration time, rather than a
    # plain lambda closing over the loop variable (which would make
    # every task fire for whichever weekday the loop landed on LAST).
    for weekday in TRACKED_WEEKDAYS:
        register_task(
            f"fellowship_checkin_{weekday}",
            weekday=weekday,
            hour=21,
            func=functools.partial(send_fellowship_checkin, weekday),
        )

    # Noon, every day -- sweeps whichever fellowship's check-in went
    # unanswered overnight, regardless of which specific day it was.
    register_task("fellowship_checkin_sweep", weekday=None, hour=12, func=process_stale_checkins)

    # 13:00, every day -- an hour after the check-in sweep, so any
    # absence the sweep itself just created has already landed before
    # this looks for absences whose reward window has passed.
    register_task("bandit_reward_computation", weekday=None, hour=13, func=compute_pending_rewards)

    # 14:00, every day -- sorts new feedback replies into themes (batched,
    # after the noon sweep and 1pm reward job). See feedback_themes.py.
    register_task("feedback_theme_sorting", weekday=None, hour=14, func=sort_pending_feedback)

    # Sunday 08:00 -- the weekly summary to every exec leader (Keziah's timing).
    register_task("weekly_leadership_digest", weekday=6, hour=8, func=send_weekly_digest)

    # Every scheduler check (~5 minutes) -- reminds and then escalates any
    # escalation case nobody has claimed; acute risk can't wait for an
    # hourly slot. See escalation.follow_up_unclaimed_cases.
    register_every_check_task("follow_up_unclaimed_escalations", follow_up_unclaimed_cases)
    # Completes a consent withdrawal that had to wait for an open acute case to
    # be claimed (and any made before anonymising existed). See withdrawal.py.
    register_every_check_task("finish_pending_withdrawals", finish_pending_withdrawals)
    # M-Pesa safety net: asks Safaricom directly about any study-guide payment still
    # pending after 2 minutes, in case its callback never arrived. See guide_payments.py.
    register_every_check_task("check_unsettled_guide_payments", check_unsettled_purchases)
    # Forgets Meta message IDs older than a week (repeat-delivery protection, webhook.py).
    register_every_check_task("forget_old_message_ids", forget_old_message_ids)
    # Number changes: an old number that doesn't answer within 24h -> a leader; expiries.
    register_every_check_task("check_number_changes", check_number_changes)

    if run_workers:
        start_scheduler()

    return app


def _should_run_background_workers():
    """
    The message workers (which answer queued WhatsApp messages) and the
    scheduler (check-ins, sweeps, escalation follow-ups) run ONLY on
    Render by default. The laptop and Render share one database, so any
    other copy of the app running them would compete with production for
    real members' incoming messages and scheduled jobs -- answering with
    whatever untested code is on the laptop. Found while fixing the
    duplicate escalation follow-up concern (2026-09-29).

    Render sets RENDER="true" on every service (documented by Render for
    exactly this purpose), so production needs no configuration.
    RUN_BACKGROUND_WORKERS=true switches them on elsewhere, deliberately.
    """
    return os.getenv("RENDER") == "true" or os.getenv("RUN_BACKGROUND_WORKERS", "").strip().lower() == "true"