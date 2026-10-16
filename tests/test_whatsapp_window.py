"""
tests/test_whatsapp_window.py

WhatsApp templates (Keziah, 2026-10-15): an ordinary message is only allowed within 24 hours of the person's
last message to us (the bot uses 23, as a margin). These tests need NO database (everything that would touch
it is replaced), so they cost no Neon compute:
    venv\\Scripts\\python -m unittest tests.test_whatsapp_window -v
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models import last_inbound
from app.routes import webhook

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class TheWindowRule(unittest.TestCase):
    def test_a_recent_message_opens_the_window(self):
        self.assertTrue(last_inbound.is_within_window(NOW - timedelta(minutes=5), NOW))
        self.assertTrue(last_inbound.is_within_window(NOW - timedelta(hours=22, minutes=59), NOW))

    def test_closed_from_23_hours_a_margin_before_metas_24(self):
        self.assertFalse(last_inbound.is_within_window(NOW - timedelta(hours=23), NOW))
        self.assertFalse(last_inbound.is_within_window(NOW - timedelta(hours=30), NOW))

    def test_someone_who_never_wrote_is_outside(self):
        self.assertFalse(last_inbound.is_within_window(None, NOW))


class TheWebhookRecordsEveryMessage(unittest.TestCase):
    def setUp(self):
        self.recorded, self.sent = [], []
        self.patches = [patch.object(webhook, "record_inbound", self.recorded.append),
                        patch.object(webhook, "claim_message_id", lambda i: True),
                        patch.object(webhook, "enqueue_message", lambda s, t: 0),
                        patch.object(webhook, "wake_workers", lambda: None),
                        patch.object(webhook, "send_whatsapp_message", lambda **k: self.sent.append(k))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_a_text_message(self):
        webhook._accept_message({"from": "254700000001", "id": "a", "type": "text", "text": {"body": "hi"}})
        self.assertEqual(self.recorded, ["254700000001"])

    def test_a_voice_note_still_opens_the_window(self):
        webhook._accept_message({"from": "254700000002", "id": "b", "type": "audio"})
        self.assertEqual(self.recorded, ["254700000002"])

    def test_a_repeat_delivery_is_not_recorded_twice(self):
        with patch.object(webhook, "claim_message_id", lambda i: False):
            webhook._accept_message({"from": "254700000003", "id": "c", "type": "text", "text": {"body": "hi"}})
        self.assertEqual(self.recorded, [])


if __name__ == "__main__":
    unittest.main()
