import unittest

from flask import Flask

from app.routes.privacy import privacy_bp


class PrivacyPageTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder="../app/templates")
        app.register_blueprint(privacy_bp)
        self.client = app.test_client()

    def test_privacy_page_is_public_and_contains_required_sections(self):
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Privacy Policy", body)
        self.assertIn("How information is used", body)
        self.assertIn("Consent and choices", body)
        self.assertIn("Retention and deletion", body)

    def test_unknown_path_is_not_served_as_privacy(self):
        response = self.client.get("/private-policy")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
