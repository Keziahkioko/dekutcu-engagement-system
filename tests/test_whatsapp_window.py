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
from app.services import whatsapp_client
from app.models import pending_outbound, member as member_model

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


class _Response:
    def __init__(self, status=200):
        self.status_code, self.text = status, "fake"


class SendingChoosesTextOrTemplate(unittest.TestCase):
    """send_whatsapp_message: inside the window -> the message; outside -> saved + ONE "message waiting" template."""

    def setUp(self):
        self.posted, self.saved, self.inside, self.meta_status = [], [], [True], [200]

        def save(to, text):
            already = any(n for (t, _x, n) in self.saved if t == to)
            self.saved.append((to, text, already))
            return already

        def mark(to):
            self.saved[:] = [(t, x, True if t == to else n) for (t, x, n) in self.saved]

        def post(payload):
            self.posted.append(payload)
            return _Response(self.meta_status[0])

        self.patches = [patch.object(whatsapp_client, "_post", post),
                        patch.object(last_inbound, "within_window", lambda n: self.inside[0]),
                        patch.object(pending_outbound, "save_outbound", save),
                        patch.object(pending_outbound, "mark_notified", mark),
                        patch.object(member_model, "get_member_by_whatsapp_id",
                                     lambda n: {"name": "Wanjiru Kamau"} if n == "254700000001" else None),
                        patch.dict("os.environ", {"WHATSAPP_TEMPLATES_ENABLED": "true"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_switched_off_everything_is_sent_as_before(self):
        self.inside[0] = False
        with patch.dict("os.environ", {"WHATSAPP_TEMPLATES_ENABLED": ""}):
            whatsapp_client.send_whatsapp_message("254700000001", "Hello")
        self.assertEqual(self.posted[0]["type"], "text")
        self.assertEqual(self.saved, [])

    def test_inside_the_window_an_ordinary_message(self):
        whatsapp_client.send_whatsapp_message("254700000001", "Hello")
        self.assertEqual([p["type"] for p in self.posted], ["text"])
        self.assertEqual(self.saved, [])

    def test_outside_the_window_saved_and_a_template_with_name_and_topic(self):
        self.inside[0] = False
        response = whatsapp_client.send_whatsapp_message("254700000001", "The new guide is out", topic="a study guide update")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["type"] for p in self.posted], ["template"])
        template = self.posted[0]["template"]
        self.assertEqual(template["name"], "dekutcu_message_waiting")
        self.assertEqual([v["text"] for v in template["components"][0]["parameters"]], ["Wanjiru", "a study guide update"])
        self.assertNotIn("The new guide is out", str(self.posted), "the real message is never in the template")
        self.assertEqual(self.saved, [("254700000001", "The new guide is out", True)])

    def test_no_topic_given_and_not_a_member(self):
        self.inside[0] = False
        whatsapp_client.send_whatsapp_message("254700000555", "Hi")
        values = [v["text"] for v in self.posted[0]["template"]["components"][0]["parameters"]]
        self.assertEqual(values, ["there", "an update"])

    def test_several_messages_only_one_template(self):
        self.inside[0] = False
        for text in ["one", "two", "three"]:
            self.assertEqual(whatsapp_client.send_whatsapp_message("254700000001", text).status_code, 200)
        self.assertEqual(len(self.posted), 1, "one template covers every waiting message")
        self.assertEqual([x for (_t, x, _n) in self.saved], ["one", "two", "three"])

    def test_meta_refuses_the_template_message_stays_saved_no_retry_loop(self):
        self.inside[0] = False
        self.meta_status[0] = 400
        response = whatsapp_client.send_whatsapp_message("254700000001", "one")
        self.assertEqual(response.status_code, 400, "the caller is told it didn't go")
        self.assertEqual(len(self.posted), 1, "tried once, not again")
        self.assertEqual(self.saved, [("254700000001", "one", False)], "still saved, for if they ever write")

    def test_window_check_fails_sends_normally(self):
        def broken(n):
            raise RuntimeError("database unreachable")
        with patch.object(last_inbound, "within_window", broken):
            whatsapp_client.send_whatsapp_message("254700000001", "Hello")
        self.assertEqual(self.posted[0]["type"], "text")


if __name__ == "__main__":
    unittest.main()
