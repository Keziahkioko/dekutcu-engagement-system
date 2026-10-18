"""
tests/test_help_guide.py

The self-taught guide (app/services/help_guide.py, Keziah 2026-10-16): the role-based numbered menu, "explain 3",
a bare "3" only right after the menu, "how do I ...?" questions, and role safety. No database (everything that
would touch it is replaced), so no Neon compute:
    venv\\Scripts\\python -m unittest tests.test_help_guide -v
"""

import json
import unittest
from unittest.mock import patch

from app.services import help_guide, intent_router, guide_coordinator

MEMBER = {"whatsapp_id": "254700000001", "name": "Wanjiru Kamau", "reg_number": "R1",
          "is_leader": False, "leads_group_label": None}
GROUP_LEADER = dict(MEMBER, leads_group_label="Bomas A")
EXEC = dict(MEMBER, is_leader=True)


def _no_coordinator():
    return patch.object(guide_coordinator, "is_coordinator", lambda m: False)


class EveryGuidePointsAtARealFeature(unittest.TestCase):
    def test_each_guides_intents_exist(self):
        for guide in help_guide.GUIDES:
            for intent in guide["intents"]:
                self.assertIn(intent, intent_router.INTENT_DEFINITIONS, f"guide '{guide['key']}' -> {intent}")

    def test_every_body_renders(self):
        for guide in help_guide.GUIDES:
            self.assertTrue(help_guide.guide_text(guide).strip())


class TheMenuDependsOnTheRole(unittest.TestCase):
    def test_a_member_sees_the_nine_approved_items_and_nothing_for_leaders(self):
        with _no_coordinator():
            text = help_guide.menu_text(MEMBER)
        self.assertTrue(text.startswith("Here's what I can help with 🙂 Reply with a number to learn more:"))
        self.assertIn("1. Questions about faith and DeKUTCU", text)
        self.assertIn("9. Your data and stopping messages", text)
        self.assertNotIn("10.", text)
        self.assertNotIn("As a", text)
        self.assertTrue(text.endswith("Say CANCEL anytime to stop what you're doing."))

    def test_a_group_leader_gets_their_section_numbered_on(self):
        with _no_coordinator():
            text = help_guide.menu_text(GROUP_LEADER)
        self.assertIn("As a group leader:\n10. Announcing to your group", text)
        self.assertNotIn("As an exec leader:", text)

    def test_someone_with_several_roles_sees_each_section_in_order(self):
        with patch.object(guide_coordinator, "is_coordinator", lambda m: True):
            text = help_guide.menu_text(dict(EXEC, leads_group_label="Bomas A"))
        self.assertLess(text.index("As a group leader:"), text.index("As the Guides Coordinator:"))
        self.assertLess(text.index("As the Guides Coordinator:"), text.index("As an exec leader:"))


class ExplainAnOption(unittest.TestCase):
    def test_ways_of_asking(self):
        for text in ["explain 3", "Explain 3", "tell me more about 3", "Tell me about 3", "option 3", "number 3",
                     "more on 3", "explain option 3", "explain #3", "explain 3 please"]:
            self.assertEqual(help_guide.menu_choice(text, after_menu=False), 3, text)

    def test_a_bare_number_only_right_after_the_menu(self):
        self.assertEqual(help_guide.menu_choice("3", after_menu=True), 3)
        self.assertIsNone(help_guide.menu_choice("3", after_menu=False))

    def test_not_menu_choices(self):
        for text in ["3 people came", "I have 3 questions", "John 3:16", "explain baptism"]:
            self.assertIsNone(help_guide.menu_choice(text, after_menu=True), text)

    def test_the_guide_and_a_number_out_of_range(self):
        with _no_coordinator():
            self.assertTrue(help_guide.explain_number(MEMBER, 3).startswith("*Events and RSVPs*"))
            self.assertIn("There's no option 15 -- pick a number from 1 to 9", help_guide.explain_number(MEMBER, 15))

    def test_a_member_can_never_reach_a_leader_guide_by_number(self):
        with _no_coordinator():
            reply = help_guide.explain_number(MEMBER, 10)
        self.assertNotIn("Announcing", reply)
        self.assertIn("There's no option 10", reply)


class TheTuesdayLineFollowsTheSetting(unittest.TestCase):
    def test_on_during_the_pilot(self):
        with patch.dict("os.environ", {"BIBLE_STUDY_SELF_REPORT": "true"}):
            self.assertIn("and Bible Study on Tuesday", help_guide.guide_text(help_guide._BY_KEY["checkins"]))
            self.assertNotIn("who was absent", help_guide.guide_text(help_guide._BY_KEY["gl_reports"]))

    def test_off(self):
        with patch.dict("os.environ", {"BIBLE_STUDY_SELF_REPORT": ""}):
            self.assertNotIn("Bible Study on Tuesday", help_guide.guide_text(help_guide._BY_KEY["checkins"]))
            self.assertIn("who was absent from Bible Study", help_guide.guide_text(help_guide._BY_KEY["gl_reports"]))


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class HowDoIQuestions(unittest.TestCase):
    def _ask(self, member, model_says):
        def fake(**kwargs):
            if isinstance(model_says, Exception):
                raise model_says
            self.listing = kwargs["messages"][1]["content"]
            return _FakeCompletion(json.dumps({"guide": model_says}))
        with _no_coordinator(), patch("app.services.llm_client.create_chat_completion", fake):
            return help_guide.answer_how_to(member, "how do I RSVP?")

    def test_the_matching_guide(self):
        self.assertTrue(self._ask(MEMBER, 3).startswith("*Events and RSVPs*"))

    def test_the_model_only_sees_this_persons_own_guides(self):
        self._ask(MEMBER, 3)
        self.assertNotIn("Announcing", self.listing)
        self.assertNotIn("10.", self.listing)

    def test_no_match_or_a_number_out_of_range_gives_the_menu(self):
        for says in [0, 15]:
            self.assertTrue(self._ask(MEMBER, says).startswith(help_guide.MENU_HEADER), says)

    def test_the_ai_failing_still_gives_the_menu(self):
        self.assertTrue(self._ask(MEMBER, RuntimeError("Groq down")).startswith(help_guide.MENU_HEADER))


class TheRouter(unittest.TestCase):
    def setUp(self):
        self.history, self.classified = [], []
        self.patches = [
            _no_coordinator(),
            patch.object(intent_router, "get_member_by_whatsapp_id", lambda n: MEMBER),
            patch.object(intent_router, "log_conversation_message", lambda w, role, text: self.history.append((role, text))),
            patch("app.models.conversation_history.get_recent_conversation",
                  lambda w, limit=10: [{"role": r, "message_text": t} for r, t in self.history[-limit:]]),
            patch.object(intent_router, "classify_intent", lambda t, w=None: self.classified.append(t) or "greeting_smalltalk"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_help_then_a_bare_number(self):
        self.assertTrue(intent_router.handle_message("254700000001", "help").startswith(help_guide.MENU_HEADER))
        self.assertTrue(intent_router.handle_message("254700000001", "5").startswith("*Buying the study guide*"))
        self.assertEqual(self.classified, [], "neither needed the AI")

    def test_a_bare_number_out_of_the_blue_goes_to_the_classifier(self):
        intent_router.handle_message("254700000001", "5")
        self.assertEqual(self.classified, ["5"])

    def test_explain_works_any_time(self):
        self.assertTrue(intent_router.handle_message("254700000001", "explain 7").startswith("*Talking to a leader*"))


if __name__ == "__main__":
    unittest.main()
