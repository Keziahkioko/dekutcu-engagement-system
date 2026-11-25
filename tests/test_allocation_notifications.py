import unittest
from unittest.mock import patch

from app.services import intent_router


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class AllocationNotifications(unittest.TestCase):
    def _run_job(self, mode, members, topup_result, ilp_result, leaders):
        sent = []

        def fake_send(to_number, message_text, **kwargs):
            sent.append((to_number, message_text, kwargs))
            return FakeResponse()

        with patch.object(intent_router, "get_data_consenting_members", return_value=members), \
             patch.object(intent_router, "set_group_labels"), \
             patch.object(intent_router.leader_assignment, "match_and_notify"), \
             patch.object(intent_router, "get_all_groups_with_leaders", return_value=leaders), \
             patch.object(intent_router, "send_whatsapp_message", side_effect=fake_send), \
             patch.object(intent_router, "allocate_members_topup", return_value=topup_result), \
             patch.object(intent_router, "allocate_members_ilp", return_value=ilp_result):
            self.assertTrue(intent_router._allocation_lock.acquire(blocking=False))
            intent_router._run_allocation_job("exec-number", mode)
            self.assertFalse(intent_router._allocation_lock.locked())

        return sent

    def test_topup_notifies_only_newly_placed_member_with_group_and_leader(self):
        members = [
            {"reg_number": "NEW", "whatsapp_id": "member-new", "name": "New Member"},
            {"reg_number": "PLACED", "whatsapp_id": "member-placed", "name": "Placed Member"},
        ]
        sent = self._run_job(
            "topup",
            members,
            {"updates": {"NEW": "Bomas #1"}, "flagged_areas": {}},
            {"groups": {}, "flagged_areas": {}},
            [("Bomas #1", "Bomas", 8, "Group Leader")],
        )

        self.assertEqual([to for to, _text, _kwargs in sent], ["member-new", "exec-number"])
        self.assertIn("Bomas #1", sent[0][1])
        self.assertIn("Group Leader", sent[0][1])
        self.assertEqual(sent[0][2]["topic"], "your Bible Study group assignment")
        self.assertIn("Group notices accepted", sent[1][1])

    def test_reshuffle_notifies_every_placed_member_and_handles_unmatched_leader(self):
        members = [
            {"reg_number": "ONE", "whatsapp_id": "member-one", "name": "Member One"},
            {"reg_number": "TWO", "whatsapp_id": "member-two", "name": "Member Two"},
        ]
        group = [dict(member) for member in members]
        sent = self._run_job(
            "reshuffle",
            members,
            {"updates": {}, "flagged_areas": {}},
            {"groups": {"Bomas": [group]}, "flagged_areas": {}},
            [("Bomas #1", "Bomas", 8, None)],
        )

        self.assertEqual([to for to, _text, _kwargs in sent], ["member-one", "member-two", "exec-number"])
        self.assertTrue(all("Bomas #1" in text for _to, text, _kwargs in sent[:2]))
        self.assertTrue(all("has not been assigned yet" in text for _to, text, _kwargs in sent[:2]))

    def test_missing_numbers_and_failed_sends_are_counted(self):
        members = [
            {"reg_number": "NO-WA", "whatsapp_id": None},
            {"reg_number": "REJECTED", "whatsapp_id": "member-rejected"},
            {"reg_number": "ERROR", "whatsapp_id": "member-error"},
        ]
        with patch.object(
            intent_router, "get_all_groups_with_leaders",
            return_value=[("Bomas #1", "Bomas", 8, "Group Leader")],
        ), patch.object(
            intent_router,
            "send_whatsapp_message",
            side_effect=[FakeResponse(400), RuntimeError("network failure")],
        ):
            result = intent_router._notify_members_of_group_assignments(
                members,
                {"NO-WA": "Bomas #1", "REJECTED": "Bomas #1", "ERROR": "Bomas #1"},
            )

        self.assertEqual(result, {"accepted": 0, "not_sent": 3})


if __name__ == "__main__":
    unittest.main()
