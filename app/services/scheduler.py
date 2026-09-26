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

_tasks = []  # (name, weekday, hour, func) -- weekday: Monday=0 ... Sunday=6, matching date.weekday()


def register_task(name, weekday, hour, func):
    _tasks.append((name, weekday, hour, func))


def _run_due_tasks():
    now = datetime.now(NAIROBI)
    today = now.date()

    for name, weekday, hour, func in _tasks:
        if now.weekday() != weekday or now.hour != hour:
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
