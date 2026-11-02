import os
import unittest
from unittest.mock import patch

from live_pocs.config import BackendMode, Settings


def environment():
    return {
        "POC_MODE": "foundry",
        "POC_BACKEND_KEY": "BACKEND_SECRET_AT_LEAST_24_CHARACTERS",
        "POC_ALLOWED_TENANTS": "customer-a,customer-b",
        "POC_FOUNDRY_GATEWAY_BASE_URL": "https://fixture.azure-api.net/model",
        "POC_MODEL_DEPLOYMENT": "shared-deployment",
        "POC_FOUNDRY_MODEL_NAME": "gpt-4.1-mini",
        "POC_FOUNDRY_TENANT_A_KEY": "TENANT_A_SECRET_VALUE",
        "POC_FOUNDRY_TENANT_B_KEY": "TENANT_B_SECRET_VALUE",
    }


class ConfigTests(unittest.TestCase):
    def test_valid_config_is_safe_by_default_and_credentials_are_not_in_repr(self):
        with patch.dict(os.environ, environment(), clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.mode, BackendMode.FOUNDRY)
        self.assertFalse(settings.live_enabled)
        self.assertEqual(settings.max_turns, 10)
        rendered = repr(settings)
        for value in ("BACKEND_SECRET", "TENANT_A_SECRET", "TENANT_B_SECRET"):
            self.assertNotIn(value, rendered)

    def test_invalid_values_do_not_silently_use_defaults(self):
        for name, value in (
            ("POC_MODE", "other"),
            ("POC_LIVE_ENABLED", "perhaps"),
            ("POC_MAX_TURNS", "11"),
            ("POC_RUN_TIMEOUT_SECONDS", "forever"),
            ("POC_ALLOWED_TENANTS", "customer-a,customer-a"),
        ):
            with self.subTest(name=name), patch.dict(
                os.environ, {**environment(), name: value}, clear=True
            ), self.assertRaises(RuntimeError):
                Settings.from_environment()

    def test_only_the_trusted_gateway_shape_is_accepted(self):
        for url in (
            "http://fixture.azure-api.net/model",
            "https://example.invalid/model",
            "https://fixture.azure-api.net/other",
            "https://user:password@fixture.azure-api.net/model",
            "https://fixture.azure-api.net/model?target=other",
            "https://fixture.azure-api.net:invalid/model",
        ):
            with self.subTest(url=url), patch.dict(
                os.environ, {**environment(), "POC_FOUNDRY_GATEWAY_BASE_URL": url}, clear=True
            ), self.assertRaises(RuntimeError):
                Settings.from_environment()

    def test_fusion_cannot_select_a_fixed_model_fallback(self):
        configured = {
            "POC_MODE": "fusion",
            "POC_BACKEND_KEY": "BACKEND_SECRET_AT_LEAST_24_CHARACTERS",
            "POC_ALLOWED_TENANTS": "customer-a,customer-b",
            "POC_GITHUB_TOKEN": "SYNTHETIC_GITHUB_TOKEN",
            "POC_GITHUB_LOGIN": "fixture-user",
            "POC_GITHUB_MODEL": "gpt-fixed-model",
        }
        with patch.dict(os.environ, configured, clear=True), self.assertRaises(RuntimeError):
            Settings.from_environment()
