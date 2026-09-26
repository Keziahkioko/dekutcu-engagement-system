"""
app/__init__.py

The app factory. This is the ONE place that builds the Flask app:
loads environment variables, registers routes (blueprints), and
initializes the database tables.

Every other file (run.py, tests) should go through create_app()
instead of building a Flask app on its own.
"""

import functools

from flask import Flask
from dotenv import load_dotenv

from app.routes.webhook import webhook_bp, start_message_worker
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
from app.services.scheduler import start_scheduler, register_task
from app.services.attendance import send_bible_study_nudges
from app.services.fellowship_checkin import send_fellowship_checkin, process_stale_checkins, TRACKED_WEEKDAYS
from app.services.bandit import compute_pending_rewards


def create_app():
    load_dotenv()  # loads .env before anything else needs those values

    app = Flask(__name__)

    # Register route blueprints
    app.register_blueprint(webhook_bp)

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

    start_message_worker()

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

    start_scheduler()

    return app