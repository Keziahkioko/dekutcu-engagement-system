"""
tests/test_pilot_outcomes.py

The ~50-volunteer pilot (Keziah, 2026-10-12): (1) BIBLE_STUDY_SELF_REPORT -- members report Bible Study
themselves, group leaders aren't asked; (2) fairer bandit outcomes -- only "I was there" or a recorded absence
count; no answer = unknown, left out. No database: every lookup is replaced. Run from the project folder:
    venv\\Scripts\\python -m unittest tests.test_pilot_outcomes -v
"""

import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from app.services import bandit, fellowship_checkin, attendance


def _no_db(*args, **kwargs):
    raise AssertionError("touched the database")


class BibleStudySelfReport(unittest.TestCase):
    def test_tuesday_is_bible_study_only_when_the_pilot_setting_is_on(self):
        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": "true"}):
            self.assertEqual(fellowship_checkin._activity_for_weekday(1), "bible_study")
        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": ""}):
            self.assertIsNone(fellowship_checkin._activity_for_weekday(1))
        self.assertEqual(fellowship_checkin._activity_for_weekday(4), "friday_fellowship", "fellowships unchanged")

    def test_setting_off_tuesday_check_in_sends_nothing(self):
        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": ""}), \
             patch.object(fellowship_checkin, "claim_checkin_broadcast", _no_db):
            self.assertIsNone(fellowship_checkin.send_fellowship_checkin(1))

    def test_setting_on_group_leaders_are_not_asked(self):
        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": "true"}), \
             patch.object(attendance, "get_data_consenting_members", _no_db):
            self.assertIsNone(attendance.send_bible_study_nudges())

    def test_tuesday_check_in_is_registered(self):
        self.assertIn(1, fellowship_checkin.TRACKED_WEEKDAYS)


class AttendedOn(unittest.TestCase):
    def _attended(self, absent=False, checked_in=False, group=None, marked=False, activity="friday_fellowship"):
        with patch.object(bandit, "absence_exists_for_date", lambda *a: absent), \
             patch("app.models.fellowship_checkin.count_checkins_on_dates", lambda *a: 1 if checked_in else 0), \
             patch("app.models.attendance_marking.marking_exists", lambda *a: marked), \
             patch("app.models.member.get_member_by_reg_number", lambda r: {"group_label": group}):
            return bandit.attended_on("R1", activity, date(2026, 10, 9))

    def test_outcomes(self):
        self.assertIs(self._attended(absent=True), False)
        self.assertIs(self._attended(checked_in=True), True)
        self.assertIsNone(self._attended(), "no answer = unknown (it used to count as attended)")
        self.assertIs(self._attended(activity="bible_study", group="G1", marked=True), True, "leader-marked, not absent")
        self.assertIsNone(self._attended(activity="bible_study", group="G1", marked=False))
        self.assertIsNone(self._attended(activity="bible_study", group=None), "ungrouped pilot member, no reply")


class Rewards(unittest.TestCase):
    def _run(self, attended, days_ago, is_control=False):
        absence = {"id": 7, "reg_number": "R1", "activity_type": "friday_fellowship", "is_control": is_control,
                   "activity_date": date.today() - timedelta(days=days_ago), "context_key": "c|1", "chosen_arm": "reminder"}
        calls = {"reward": [], "unknown": [], "posterior": []}
        with patch.object(bandit, "get_absences_awaiting_reward", lambda cutoff: [absence]), \
             patch.object(bandit, "attended_on", lambda *a: attended), \
             patch.object(bandit, "set_reward", lambda i, r: calls["reward"].append(r)), \
             patch.object(bandit, "mark_outcome_unknown", lambda i, w: calls["unknown"].append(w)), \
             patch.object(bandit, "update_posterior", lambda *a: calls["posterior"].append(a)), \
             patch.object(bandit, "_compute_pending_recovery", lambda: None):
            bandit.compute_pending_rewards()
        return calls

    def test_came_back_and_missed_are_learned_from(self):
        self.assertEqual(self._run(True, 8)["reward"], [True])
        self.assertEqual(len(self._run(True, 8)["posterior"]), 1)
        self.assertEqual(self._run(False, 8)["reward"], [False])

    def test_no_answer_waits_for_a_late_reply_then_is_unknown(self):
        waiting = self._run(None, 9)          # next session 2 days ago -- a late "I was there" can still come
        self.assertEqual((waiting["reward"], waiting["unknown"], waiting["posterior"]), ([], [], []))
        gave_up = self._run(None, 14)         # next session 7 days ago -- the next check-in has replaced it
        self.assertEqual((gave_up["reward"], gave_up["unknown"], gave_up["posterior"]), ([], ["outcome"], []),
                         "unknown: not a success, not a failure, not learned from")

    def test_control_group_never_teaches_the_bandit(self):
        self.assertEqual(self._run(True, 8, is_control=True)["posterior"], [])


class Recovery(unittest.TestCase):
    def _run(self, first, second, days_ago=16):
        absence = {"id": 7, "reg_number": "R1", "activity_type": "friday_fellowship", "reward": first,
                   "activity_date": date.today() - timedelta(days=days_ago)}
        result = {"recovered": [], "unknown": []}
        with patch.object(bandit, "get_absences_awaiting_recovery", lambda cutoff: [absence]), \
             patch.object(bandit, "attended_on", lambda *a: second), \
             patch.object(bandit, "set_recovered_within_2", lambda i, r: result["recovered"].append(r)), \
             patch.object(bandit, "mark_outcome_unknown", lambda i, w: result["unknown"].append(w)):
            bandit._compute_pending_recovery()
        return result

    def test_recovery_rules(self):
        self.assertEqual(self._run(True, None)["recovered"], [True], "came back the very next time")
        self.assertEqual(self._run(False, True)["recovered"], [True])
        self.assertEqual(self._run(None, True)["recovered"], [True], "first unknown, second attended")
        self.assertEqual(self._run(False, False)["recovered"], [False], "known to have missed both")
        self.assertEqual(self._run(None, False, days_ago=21)["unknown"], ["recovery"], "first unknown -> can't say")
        self.assertEqual(self._run(False, None, days_ago=21)["unknown"], ["recovery"])
        waiting = self._run(False, None, days_ago=16)
        self.assertEqual((waiting["recovered"], waiting["unknown"]), ([], []), "still waiting for a late answer")


if __name__ == "__main__":
    unittest.main()
