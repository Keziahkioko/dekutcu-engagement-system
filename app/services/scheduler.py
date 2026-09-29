"""
app/services/scheduler.py

Stage 7: the first PROACTIVE trigger in this project -- everything
else only ever runs in response to an incoming WhatsApp message. This
is a small, generic scheduled-task runner: register a task with a
day-of-week + hour it's due, and a function to call. A single
background thread (started in app/__init__.py, same pattern as the
message-queue workers in webhook.py) wakes up periodically and fires
any task whose window has arrived, guarded against double-firing by
scheduled_task_run.py -- an hour-wide check window could otherwise
fire the same task more than once.

Runs in Africa/Nairobi time explicitly -- the server itself runs in
UTC (Render's default), but "9pm" means 9pm Nairobi time from a real
user's perspective, not 9pm UTC.
"""

import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.scheduled_task_run import get_last_run_date, mark_task_run

NAIROBI = ZoneInfo("Africa/Nairobi")

_CHECK_INTERVAL_SECONDS = 300  # 5 minutes -- hour-level precision is enough, nothing finer is needed

_tasks = []  # (name, weekday, hour, func) -- weekday: Monday=0 ... Sunday=6 (matching date.weekday()), or None for "every day"

# Stage 13: tasks that run on EVERY check (every _CHECK_INTERVAL_SECONDS),
# not at a fixed hour -- first needed for following up unclaimed
# escalations, where acute risk can't wait for the next hour's slot. They
# must be safe to run repeatedly: they track their own progress (e.g.
# escalation_cases.reminded_at) rather than relying on run-once dedup.
_every_check_tasks = []  # (name, func)


def register_task(name, weekday, hour, func):
    """weekday=None means the task runs every day at this hour."""
    _tasks.append((name, weekday, hour, func))


def register_every_check_task(name, func):
    """Runs on every scheduler check (every few minutes). func must be safe to repeat."""
    _every_check_tasks.append((name, func))


def _run_due_tasks():
    for name, func in _every_check_tasks:
        try:
            func()
        except Exception as e:
            print(f"Every-check task '{name}' failed: {e}")

    now = datetime.now(NAIROBI)
    today = now.date()

    for name, weekday, hour, func in _tasks:
        if weekday is not None and now.weekday() != weekday:
            continue
        if now.hour != hour:
            continue
        if get_last_run_date(name) == today:
            continue  # already fired today, within this same hour-long window

        try:
            func()
        except Exception as e:
            print(f"Scheduled task '{name}' failed: {e}")
        mark_task_run(name, today)


def _scheduler_loop():
    while True:
        try:
            _run_due_tasks()
        except Exception as e:
            print(f"Scheduler loop error: {e}")
        time.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()
