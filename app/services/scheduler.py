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

# How often to check (Keziah, 2026-10-12 -- Neon free-plan compute): every check touches the database,
# and Neon only sleeps after 5 quiet minutes, so the old 5-minute rhythm alone kept it awake around the
# clock. Now: just after each hour (the daily tasks only need the hour), and every 5 minutes ONLY while
# something time-critical is waiting -- an urgent care case (30-minute reminder, 2-hour backstop) or an
# M-Pesa prompt (2-minute double-check) -- see check_often_for(). A normal case's 2-hour reminder can
# therefore arrive up to an hour late (2-3 hours); urgent cases are unaffected.
_FAST_SECONDS = 300
_fast_until = [0.0]   # time.time() until which checks run every _FAST_SECONDS

_tasks = []  # (name, weekday, hour, func) -- weekday: Monday=0 ... Sunday=6 (matching date.weekday()), or None for "every day"

# Stage 13: tasks that run on EVERY check (hourly, or every 5 minutes while busy),
# not at a fixed hour -- first needed for following up unclaimed
# escalations, where acute risk can't wait for the next hour's slot. They
# must be safe to run repeatedly: they track their own progress (e.g.
# escalation_cases.reminded_at) rather than relying on run-once dedup.
_every_check_tasks = []  # (name, func)


def register_task(name, weekday, hour, func):
    """weekday=None means the task runs every day at this hour."""
    _tasks.append((name, weekday, hour, func))


def register_every_check_task(name, func):
    """Runs on every scheduler check (hourly, or every 5 minutes while busy). func must be safe to repeat."""
    _every_check_tasks.append((name, func))


def check_often_for(minutes):
    """Check every 5 minutes for the next `minutes` -- called when something time-critical starts."""
    _fast_until[0] = max(_fast_until[0], time.time() + minutes * 60)


def _seconds_until_next_check(now_ts=None):
    now_ts = time.time() if now_ts is None else now_ts
    if now_ts < _fast_until[0]:
        return _FAST_SECONDS
    # Just after the next hour begins (1 minute in), so each hour's slot is visited once and none skipped.
    return 3600 - (now_ts % 3600) + 60


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
        time.sleep(_seconds_until_next_check())


def start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()
