import unittest
from unittest.mock import patch

from app.services import mpesa, study_guides


class MpesaPromptDiagnostics(unittest.TestCase):
    def test_stk_refusal_includes_safe_status_and_response_detail(self):
        response = {
            "ResponseCode": "1",
            "errorMessage": "Invalid phone number 254712345678; passkey: sensitive-value",
        }
        with patch.object(mpesa, "_post", return_value=(400, response)):
            with self.assertRaises(mpesa.MpesaError) as raised:
                mpesa.stk_push("254700000001", 70, "DeKUTCU", "Study guide")

        diagnostic = raised.exception.diagnostic
        self.assertIn("HTTP 400", diagnostic)
        self.assertIn("code 1", diagnostic)
        self.assertIn("Invalid phone number", diagnostic)
        self.assertIn("[number redacted]", diagnostic)
        self.assertIn("[credential redacted]", diagnostic)
        self.assertNotIn("254712345678", diagnostic)
        self.assertNotIn("254700000001", diagnostic)
        self.assertNotIn("sensitive-value", diagnostic)

    def test_purchase_failure_logs_diagnostic_and_keeps_generic_member_reply(self):
        whatsapp_id = "254700000001"
        pending = {
            "created_at": study_guides._now().replace(tzinfo=None),
            "guide_id": 1,
        }
        member = {"whatsapp_id": whatsapp_id, "reg_number": "R1"}
        guide = {"id": 1, "title": "Bible Study Guide", "price_kes": 70}
        diagnostic = "STK push refused (HTTP 400, code 1): Invalid shortcode"

        with (
            patch.object(study_guides, "get_pending_guide_purchase", return_value=pending),
            patch.object(study_guides, "delete_pending_guide_purchase"),
            patch.object(study_guides, "get_member_by_whatsapp_id", return_value=member),
            patch.object(study_guides, "get_guide", return_value=guide),
            patch.object(study_guides, "get_current_guide", return_value=guide),
            patch.object(study_guides, "get_paid_purchase", return_value=None),
            patch.object(study_guides.guide_payments, "start_payment",
                         side_effect=mpesa.MpesaError("refused", diagnostic)),
            self.assertLogs("app.services.study_guides", level="WARNING") as logs,
        ):
            reply = study_guides.handle_purchase_reply(whatsapp_id, "YES")

        self.assertEqual(
            reply,
            "I couldn't send the M-Pesa prompt just now -- please try again in a few minutes.",
        )
        self.assertIn(diagnostic, logs.output[0])
        self.assertNotIn(whatsapp_id, logs.output[0])


if __name__ == "__main__":
    unittest.main()
