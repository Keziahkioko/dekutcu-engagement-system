"""
tests/test_database_wakeups.py

Neon free-plan compute (Keziah, 2026-10-12, at 84% of the month's 100 hours on day 12): three things kept
the database awake around the clock -- the message workers polling every half second, the scheduler
checking every 5 minutes, and the pool holding connections open forever. These tests check each fix
WITHOUT a database (everything that would touch it is replaced), so they cost no Neon compute:
    venv\\Scripts\\python -m unittest tests.test_database_wakeups -v
"""

import threading
import time
import unittest
from unittest.mock import patch

from app import database
from app.routes import webhook
from app.services import scheduler, escalation


class WorkersWaitForASignal(unittest.TestCase):
    def setUp(self):
        self.queue, self.claims, self.handled = [], [0], []

        def claim(shard, workers):
            self.claims[0] += 1
            return self.queue.pop(0) if self.queue else None
        self.patches = [patch.object(webhook, "claim_next_message", claim),
                        patch.object(webhook, "route_incoming_message", lambda s, t: f"reply to {t}"),
                        patch.object(webhook, "send_whatsapp_message", lambda **k: self.handled.append(k["message_text"])),
                        patch.object(webhook, "delete_pending_message", lambda i: None)]
        for p in self.patches:
            p.start()
        threading.Thread(target=webhook._process_queue_shard, args=(0,), daemon=True).start()
        time.sleep(0.3)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_an_idle_worker_makes_no_database_queries(self):
        before = self.claims[0]
        time.sleep(2)   # the old loop would have asked the database ~4 times in this time
        self.assertEqual(self.claims[0], before, "no polling while idle")

    def test_a_queued_message_is_handled_at_once_when_woken(self):
        self.queue.append({"id": 1, "sender_number": "254700000001", "message_text": "hi"})
        webhook.wake_workers()
        time.sleep(0.5)
        self.assertEqual(self.handled, ["reply to hi"])


class AWorkerSurvivesADroppedConnection(unittest.TestCase):
    """Live bug, 2026-10-16: a database error while checking the queue killed the worker thread for good."""

    def test_the_worker_retries_and_still_answers(self):
        handled, calls = [], [0]

        def claim(shard, workers):
            calls[0] += 1
            if calls[0] == 1:
                raise ConnectionError("server closed the connection unexpectedly")
            if calls[0] == 2:
                return {"id": 1, "sender_number": "254700000001", "message_text": "hi"}
            return None
        with patch.object(webhook, "claim_next_message", claim), \
             patch.object(webhook, "_CLAIM_RETRY_SECONDS", 0.1), \
             patch.object(webhook, "route_incoming_message", lambda s, t: f"reply to {t}"), \
             patch.object(webhook, "send_whatsapp_message", lambda **k: handled.append(k["message_text"])), \
             patch.object(webhook, "delete_pending_message", lambda i: None):
            worker = threading.Thread(target=webhook._process_queue_shard, args=(1,), daemon=True)
            worker.start()
            time.sleep(0.6)
            self.assertTrue(worker.is_alive(), "the worker thread is still running")
            self.assertEqual(handled, ["reply to hi"], "the message queued during the failure was answered")


class WorkersRunInTheProcessThatReceivesMessages(unittest.TestCase):
    """
    Live bug, 2026-10-18: Render's gunicorn --preload builds the app (and starts the workers) in a parent
    process, then forks a child to receive webhooks. The child's wake-up reached nobody; replies waited for
    the parent's hourly scheduler. Simulated here by changing what os.getpid() says.
    """

    def setUp(self):
        self.started = []
        self.saved = (webhook._workers_enabled[0], webhook._workers_pid[0])
        self.patches = [patch.object(webhook, "start_message_worker",
                                     lambda: (self.started.append(webhook.os.getpid()),
                                              webhook._workers_pid.__setitem__(0, webhook.os.getpid())))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        webhook._workers_enabled[0], webhook._workers_pid[0] = self.saved

    def test_a_forked_child_starts_its_own_workers_once(self):
        webhook._workers_enabled[0], webhook._workers_pid[0] = True, 1000          # built in the parent, pid 1000
        with patch.object(webhook.os, "getpid", lambda: 2000):                     # the child receiving webhooks
            webhook.wake_workers()
            webhook.wake_workers()
        self.assertEqual(self.started, [2000], "started in the child, and only once")

    def test_the_same_process_starts_nothing_new(self):
        webhook._workers_enabled[0], webhook._workers_pid[0] = True, webhook.os.getpid()
        webhook.wake_workers()
        self.assertEqual(self.started, [])

    def test_never_where_workers_were_never_switched_on(self):
        webhook._workers_enabled[0], webhook._workers_pid[0] = False, None         # e.g. the laptop
        with patch.object(webhook.os, "getpid", lambda: 2000):
            webhook.wake_workers()
        self.assertEqual(self.started, [])


class EachProcessGetsItsOwnConnections(unittest.TestCase):
    def setUp(self):
        self.saved = (database._pool, database._pool_pid, list(database._inherited_pools))
        self.made = []

        def fake_pool(**kwargs):
            pool = _FakePool([])
            self.made.append(pool)
            return pool
        self.patches = [patch.object(database.psycopg2.pool, "ThreadedConnectionPool", fake_pool),
                        patch.object(database.threading, "Thread", lambda **k: type("T", (), {"start": lambda s: None})())]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        database._pool, database._pool_pid, database._inherited_pools[:] = self.saved

    def test_a_child_does_not_use_the_parents_connections(self):
        parents = _FakePool([_FakeConn()])
        database._pool, database._pool_pid = parents, 1000
        with patch.object(database.os, "getpid", lambda: 2000):
            mine = database._get_pool()
            again = database._get_pool()
        self.assertIsNot(mine, parents)
        self.assertIs(again, mine, "built once per process")
        self.assertIn(parents, database._inherited_pools, "kept, not closed -- closing would cut the parent's connections")
        self.assertFalse(parents._pool[0].closed)

    def test_the_same_process_keeps_its_pool(self):
        current = _FakePool([])
        database._pool, database._pool_pid = current, database.os.getpid()
        self.assertIs(database._get_pool(), current)
        self.assertEqual(self.made, [])


class SendsToMetaHaveATimeLimit(unittest.TestCase):
    def test_a_timeout_is_passed(self):
        from app.services import whatsapp_client
        seen = {}

        class _Ok:
            status_code, text = 200, ""
        with patch.object(whatsapp_client.requests, "post", lambda *a, **k: seen.update(k) or _Ok()):
            whatsapp_client.send_whatsapp_message("254700000001", "hi")
        self.assertEqual(seen.get("timeout"), 30)


class SchedulerRhythm(unittest.TestCase):
    def setUp(self):
        scheduler._fast_until[0] = 0.0

    def test_hourly_by_default_just_after_the_hour(self):
        at_10_20 = 1_800_000_000 - (1_800_000_000 % 3600) + 20 * 60      # 20 minutes past some hour
        self.assertEqual(scheduler._seconds_until_next_check(at_10_20), 40 * 60 + 60, "next check at 1 minute past the hour")

    def test_every_five_minutes_while_something_urgent_is_waiting(self):
        scheduler.check_often_for(150)
        self.assertEqual(scheduler._seconds_until_next_check(), 300)
        later = time.time() + 151 * 60
        self.assertEqual(scheduler._seconds_until_next_check(later), 3600 - (later % 3600) + 60,
                         "back to hourly afterwards (1 minute past the next hour -- which can be under 5 minutes away)")

    def test_an_urgent_case_switches_on_the_fast_checks(self):
        with patch.object(escalation, "find_target_leaders", lambda m: [("254700000009", "Leader", "R9")]), \
             patch.object(escalation, "create_case", lambda *a, **k: 1), \
             patch.object(escalation, "create_escalation", lambda *a, **k: None), \
             patch.object(escalation, "send_whatsapp_message", lambda *a, **k: None):
            escalation._notify_leaders({"name": "M", "whatsapp_id": "254700000001", "reg_number": "R1"},
                                       "needs_support", "x", "may need urgent support", acute=True)
        self.assertEqual(scheduler._seconds_until_next_check(), 300)

    def test_a_normal_case_does_not(self):
        with patch.object(escalation, "find_target_leaders", lambda m: [("254700000009", "Leader", "R9")]), \
             patch.object(escalation, "create_case", lambda *a, **k: 1), \
             patch.object(escalation, "create_escalation", lambda *a, **k: None), \
             patch.object(escalation, "send_whatsapp_message", lambda *a, **k: None):
            escalation._notify_leaders({"name": "M", "whatsapp_id": "254700000001", "reg_number": "R1"},
                                       "needs_support", "x", "would like support", acute=False)
        # Checked on the switch itself, not the wait: just before the hour, the hourly wait is legitimately < 5 min.
        self.assertEqual(scheduler._fast_until[0], 0.0, "fast checks not switched on")


class _FakeConn:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed = 1


class _FakePool:
    def __init__(self, conns):
        self._lock = threading.Lock()
        self._pool = list(conns)


class IdleConnectionsAreClosed(unittest.TestCase):
    def test_only_connections_idle_over_two_minutes_close(self):
        old, recent, never_used = _FakeConn(), _FakeConn(), _FakeConn()
        pool = _FakePool([old, recent, never_used])
        now = 10_000.0
        database._last_used[id(old)] = now - 200
        database._last_used[id(recent)] = now - 30
        self.assertEqual(database._close_idle_connections(pool, now), 1)
        self.assertTrue(old.closed)
        self.assertFalse(recent.closed)
        self.assertFalse(never_used.closed, "a pre-warmed connection starts its idle clock instead")
        self.assertEqual(pool._pool, [recent, never_used])
        self.assertEqual(database._close_idle_connections(pool, now + 130), 2, "after two quiet minutes, all closed")
        self.assertEqual(pool._pool, [])


if __name__ == "__main__":
    unittest.main()
