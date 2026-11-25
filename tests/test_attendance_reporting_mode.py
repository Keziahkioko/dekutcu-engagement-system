"""Attendance report behavior for leader-marking and self-report modes; no database required."""

import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader
from app.services import dashboard, reporting


class AttendanceSummaryModeTests(unittest.TestCase):
    def test_self_report_summary_uses_reply_counts_and_excludes_bible_study_from_fellowships(self):
        queries = []

        def fake_query(sql, params=()):
            queries.append(sql)
            if "LEFT JOIN fellowship_checkins c" in sql and "b.activity_type = 'bible_study'" in sql:
                return [{"sessions": 5, "said_present": 1, "absences": 0}]
            if "GROUP BY b.activity_type" in sql:
                return [{
                    "activity_type": "friday_fellowship",
                    "sessions": 2,
                    "said_present": 3,
                    "absences": 1,
                }]
            raise AssertionError(f"Unexpected query: {sql}")

        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": "true"}), \
             patch.object(reporting, "_q", side_effect=fake_query), \
             patch.object(reporting, "display_name_for", side_effect=lambda activity: activity):
            result = reporting.attendance_summary(30)

        self.assertTrue(result["self_report_mode"])
        self.assertEqual(result["bible_study"], [])
        self.assertEqual(result["bible_study_self_report"], {
            "check_in_sessions": 5,
            "present_replies": 1,
            "absences_recorded": 0,
        })
        self.assertEqual(result["groups_with_no_marking"], [])
        self.assertEqual([row["activity"] for row in result["fellowships"]], ["friday_fellowship"])
        self.assertNotIn("attendance_markings", "\n".join(queries))
        self.assertIn("b.activity_type <> 'bible_study'", queries[-1])

    def test_group_summary_does_not_expose_orgwide_self_report_counts(self):
        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": "true"}), \
             patch.object(reporting, "_q") as query:
            result = reporting.attendance_summary(30, group_label="Group A")

        query.assert_not_called()
        self.assertTrue(result["self_report_mode"])
        self.assertFalse(result["group_level_self_report_available"])
        self.assertNotIn("bible_study_self_report", result)

    def test_leadership_tools_explain_self_report_mode(self):
        member = {"is_leader": True, "leads_group_label": "Group A"}
        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": "true"}):
            tools, _ = reporting._build_tools(member)

        descriptions = {tool["function"]["name"]: tool["function"]["description"] for tool in tools}
        self.assertIn("self-report session and response counts", descriptions["attendance_summary"])
        self.assertIn("not available by group", descriptions["my_group_report"])

    def test_leader_marking_summary_is_preserved_when_self_report_is_off(self):
        def fake_query(sql, params=()):
            if "FROM attendance_markings WHERE" in sql and "GROUP BY group_label" in sql:
                return [{
                    "group_label": "Group A",
                    "sessions_marked": 2,
                    "roster_total": 10,
                    "absent_total": 2,
                }]
            if "SELECT DISTINCT group_label FROM members" in sql:
                return [{"group_label": "Group B"}]
            if "FROM checkin_broadcasts b" in sql:
                return []
            raise AssertionError(f"Unexpected query: {sql}")

        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": ""}), \
             patch.object(reporting, "_q", side_effect=fake_query):
            result = reporting.attendance_summary(30)

        self.assertFalse(result["self_report_mode"])
        self.assertEqual(result["bible_study"][0]["attendance_rate_percent"], 80)
        self.assertEqual(result["groups_with_no_marking"], ["Group B"])

    def test_group_lapsing_report_does_not_attribute_bible_study_self_reports_by_current_group(self):
        queries = []

        def fake_query(sql, params=()):
            queries.append(sql)
            return []

        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": "true"}), \
             patch.object(reporting, "_q", side_effect=fake_query):
            result = reporting.lapsing_members(group_label="Group A")

        self.assertEqual(result, {"members": []})
        self.assertIn("a.activity_type <> 'bible_study'", queries[0])


class DashboardSelfReportTests(unittest.TestCase):
    def test_weekly_self_report_data_has_counts_not_a_rate_and_is_not_group_scoped(self):
        with patch.dict(os.environ, {"BIBLE_STUDY_SELF_REPORT": "true"}), \
             patch.object(dashboard, "_q", return_value=[{
                 "week": date(2026, 11, 23),
                 "sessions": 1,
                 "present_replies": 1,
             }]) as query:
            result = dashboard.bible_study_weekly(30)
            group_result = dashboard.bible_study_weekly(30, group_label="Group A")

        self.assertEqual(result, [{
            "week": "2026-11-23",
            "sessions": 1,
            "present_replies": 1,
        }])
        self.assertNotIn("rate", result[0])
        self.assertEqual(group_result, [])
        query.assert_called_once()

    def test_weekly_digest_uses_self_report_wording(self):
        feedback = {
            "overall_response_rate_percent": None,
            "themes": {},
            "questions_relayed_to_leaders": {"awaiting_answer_anonymous": []},
        }
        with patch.object(reporting, "attendance_summary", return_value={
                 "self_report_mode": True,
                 "bible_study": [],
                 "bible_study_self_report": {
                     "check_in_sessions": 5,
                     "present_replies": 1,
                     "absences_recorded": 0,
                 },
                 "groups_with_no_marking": [],
                 "fellowships": [],
             }), \
             patch.object(reporting, "lapsing_members", return_value={"members": []}), \
             patch.object(reporting, "escalation_summary", return_value={
                 "open_unclaimed_cases": [],
                 "total_cases": 0,
             }), \
             patch.object(reporting, "feedback_summary", return_value=feedback), \
             patch.object(reporting, "companion_questions", return_value={
                 "not_covered_general_questions_anonymous": [],
             }), \
             patch.object(reporting, "study_guide_summary", return_value={"study_guides": []}):
            digest = reporting.build_weekly_digest()

        self.assertIn("Bible Study: 1 self-reported present replies across 5 self-report check-in sessions", digest)
        self.assertNotIn("no attendance was marked", digest)
        self.assertNotIn("Groups with no marking", digest)


class AttendanceTemplateFallbackTests(unittest.TestCase):
    def test_weekly_values_are_rendered_as_html_tables_without_chart_javascript(self):
        template_root = Path(__file__).resolve().parents[1] / "app" / "templates"
        environment = Environment(loader=FileSystemLoader(str(template_root)))
        environment.globals["url_for"] = lambda *args, **kwargs: "/"
        template = environment.get_template("dashboard/attendance.html")
        html = template.render(
            attendance={
                "self_report_mode": True,
                "bible_study": [],
                "groups_with_no_marking": [],
                "bible_study_self_report": {
                    "check_in_sessions": 1,
                    "present_replies": 3,
                    "absences_recorded": 0,
                },
            },
            bible_study_weekly=[{
                "week": "2026-11-23",
                "sessions": 1,
                "present_replies": 3,
            }],
            fellowship_weekly={
                "weeks": ["2026-11-23"],
                "series": {"Friday Fellowship": [2]},
            },
            lapsing=[],
            days=7,
            periods={7: "Last 7 days"},
            viewer={
                "name": "Leader",
                "is_leader": True,
                "leads_group_label": None,
                "exec_office": None,
            },
            nav_pages=[("Home", "dashboard.overview", "home")],
            active="attendance",
            demo_mode=False,
        )

        self.assertIn(".chart.pending canvas { display: none; }", html)
        self.assertIn('class="chart pending"', html)
        self.assertIn("Present replies", html)
        self.assertIn("<td class=\"num\">3</td>", html)
        self.assertIn("Friday Fellowship", html)
        self.assertIn("<td class=\"num\">2</td>", html)


if __name__ == "__main__":
    unittest.main()
