import unittest
from urllib.parse import quote, quote_plus

from app.utils.redaction import is_sensitive_key, redact_secrets


class RedactionTests(unittest.TestCase):
    def test_recursively_redacts_encoded_environment_values_without_mutation(self):
        secret = "fixture token/with+symbols=42"
        variants = (secret, quote(secret, safe=""), quote_plus(secret, safe=""))
        original = {"errors": list(variants), "price": 12.5, "ready": False}

        redacted = redact_secrets(original, {"PROVIDER_API_KEY": secret})

        self.assertEqual(redacted["errors"], ["[REDACTED]"] * len(variants))
        self.assertEqual(original["errors"], list(variants))
        self.assertEqual(redacted["price"], 12.5)
        self.assertIs(redacted["ready"], False)

    def test_redaction_is_idempotent_for_inline_credentials(self):
        value = {"errors": [
            'Authorization: Bearer fixture-token',
            '{"client_secret": "fixture with spaces"}',
            "api-key=fixture-key&symbol=600000",
            "https://fixture-user:fixture-password@example.test/path",
            "https://open.larksuite.com/open-apis/bot/v2/hook/fixture-hook",
        ]}

        redacted = redact_secrets(value, {})

        self.assertNotIn("fixture", str(redacted))
        self.assertIn("symbol=600000", str(redacted))
        self.assertEqual(redact_secrets(redacted, {}), redacted)

    def test_noncredential_environment_values_and_diagnostics_are_preserved(self):
        value = {"report": "WAIT: provider offline; cache_key=abc; quantity=100"}
        self.assertEqual(redact_secrets(value, {"APP_NAME": "provider"}), value)

    def test_short_configured_credentials_are_not_exempt_from_redaction(self):
        for secret in ("a", "1", "xy"):
            with self.subTest(secret=secret):
                environment = {"TOKEN": secret}
                self.assertEqual(redact_secrets(secret, environment), "[REDACTED]")
                self.assertEqual(
                    redact_secrets("[REDACTED]", environment), "[REDACTED]"
                )
                self.assertEqual(redact_secrets(100, environment), 100)

    def test_sensitive_key_classification_does_not_reject_ordinary_metadata(self):
        for key in ("api_key", "appSecret", "HITHINK_FINANCE_API_KEY", "Authorization", "TUSHARE_TOKEN"):
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_key(key))
        for key in ("cache_key", "source_sha", "run_id", "data_cutoff"):
            with self.subTest(key=key):
                self.assertFalse(is_sensitive_key(key))


if __name__ == "__main__":
    unittest.main()
