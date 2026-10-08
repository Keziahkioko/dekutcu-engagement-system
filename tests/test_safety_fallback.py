"""
tests/test_safety_fallback.py

What the bot does when the AI safety check or the intent classifier FAILS (Keziah, 2026-10-08). Two kinds:
  1. the AI refused to label the message (Groq: "json_validate_failed", carrying the AI's own text) -> stay
     cautious (crisis-like AI text = urgent, otherwise distress / needs_support);
  2. the service is down (network, quota, server) -> keyword safety net, no false alarm for ordinary messages.
No database, no real AI -- every AI call is replaced. Run from the project folder:
    venv\\Scripts\\python -m unittest tests.test_safety_fallback -v
"""

import unittest
from unittest.mock import patch

from app.services import escalation, intent_router


class _Answer:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


def _raise(error):
    def call(*args, **kwargs):
        raise error
    return call


class BadRequestError(Exception):
    """Same class NAME as the Groq/OpenAI error a refusal arrives as."""


REFUSAL_CRISIS = BadRequestError("Error code: 400 - {'error': {'code': 'json_validate_failed', 'failed_generation': "
                                 "\"I'm really sorry you're feeling this way. If you're thinking about suicide, please "
                                 "contact a crisis hotline -- you're not alone.\"}}")
REFUSAL_PLAIN = BadRequestError("Error code: 400 - {'error': {'code': 'json_validate_failed', 'failed_generation': 'Romans 99'}}")


class KeywordSafetyNet(unittest.TestCase):
    def test_urgent_phrases(self):
        for text in ["I want to kill myself", "I just want to END MY LIFE", "thinking about suicide", "I'm suicidal",
                     "I don't want to live anymore", "I don’t want to be alive", "nataka kujiua", "Sitaki kuishi tena",
                     "bora nife", "I keep wanting to hurt myself", "self-harm again"]:
            with self.subTest(text=text):
                self.assertEqual(escalation.keyword_severity(text), "acute_risk")

    def test_distress_phrases(self):
        for text in ["I feel hopeless", "I'm so depressed", "I can't cope anymore", "nobody cares about me",
                     "Nimechoka na maisha", "nimekata tamaa", "niko down sana", "everything is falling apart"]:
            with self.subTest(text=text):
                self.assertEqual(escalation.keyword_severity(text), "distress")

    def test_everyday_messages_are_not_alarms(self):
        for text in ["What time is fellowship?", "I'm so tired after the CAT", "CAT stress is real", "I died laughing",
                     "Romans 99:99", "How do I pay for the guide?", "Hi", "", "niko na stress ya exams"]:
            with self.subTest(text=text):
                self.assertEqual(escalation.keyword_severity(text), "none")


class SafetyCheckFallback(unittest.TestCase):
    def _assess(self, text, behaviour):
        with patch.object(escalation, "create_chat_completion", behaviour):
            return escalation.assess_severity(text)

    def test_service_down_ordinary_message_is_not_a_false_alarm(self):
        for error in [ConnectionError("network down"), TimeoutError("timed out"), RuntimeError("No LLM provider available")]:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self._assess("What time is fellowship?", _raise(error)), "none")

    def test_service_down_still_catches_urgent_and_distress_words(self):
        self.assertEqual(self._assess("I want to end my life", _raise(ConnectionError())), "acute_risk")
        self.assertEqual(self._assess("I feel hopeless", _raise(ConnectionError())), "distress")

    def test_refusal_with_crisis_text_is_urgent(self):
        self.assertEqual(self._assess("I can't do this anymore", _raise(REFUSAL_CRISIS)), "acute_risk")

    def test_refusal_without_crisis_text_stays_cautious(self):
        self.assertEqual(self._assess("Romans 99:99", _raise(REFUSAL_PLAIN)), "distress")

    def test_answer_that_is_not_the_label_is_treated_like_a_refusal(self):
        self.assertEqual(self._assess("hi", lambda **k: _Answer("I'm here for you. Please call a crisis helpline.")), "acute_risk")
        self.assertEqual(self._assess("hi", lambda **k: _Answer("hello there")), "distress")

    def test_normal_answers_unchanged(self):
        for label in ["none", "distress", "acute_risk"]:
            with self.subTest(label=label):
                self.assertEqual(self._assess("x", lambda **k: _Answer(f'{{"severity": "{label}"}}')), label)


class ClassifierFallback(unittest.TestCase):
    def _classify(self, behaviour):
        with patch.object(intent_router, "create_chat_completion", behaviour):
            return intent_router.classify_intent("What time is fellowship?")   # no WhatsApp id: no history, no database

    def test_service_down_is_service_unavailable(self):
        self.assertEqual(self._classify(_raise(ConnectionError("network down"))), "service_unavailable")

    def test_refusal_still_goes_to_support(self):
        self.assertEqual(self._classify(_raise(REFUSAL_CRISIS)), "needs_support")

    def test_service_unavailable_is_never_a_label_the_ai_can_choose(self):
        self.assertNotIn("service_unavailable", intent_router.VALID_INTENTS)


if __name__ == "__main__":
    unittest.main()
