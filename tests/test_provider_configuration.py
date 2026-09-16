import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi_app.main import DaytonaSandboxManager


class ProviderConfigurationTests(unittest.TestCase):
    def test_generic_provider_uses_codex_api_key_without_exposing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "DAYTONA_API_KEY": "daytona-secret",
                "CODEX_API_KEY": "nebius-secret",
                "CODEX_PROVIDER": "nebius",
                "CODEX_PROVIDER_NAME": "Nebius AI Studio",
                "CODEX_BASE_URL": "https://provider.example/v1/",
                "CODEX_MODEL": "provider-model",
                "SANDBOX_STATE_FILE": str(Path(directory) / "state"),
            },
            clear=True,
        ):
            manager = DaytonaSandboxManager()
            settings = manager._settings()
            status = manager.status()

        self.assertEqual(settings["provider"], "nebius")
        self.assertEqual(settings["provider_name"], "Nebius AI Studio")
        self.assertEqual(settings["provider_key"], "nebius-secret")
        self.assertTrue(status["configured"])
        self.assertNotIn("nebius-secret", str(status))

    def test_provider_identifier_is_constrained_before_codex_config_is_built(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DAYTONA_API_KEY": "daytona-secret",
                "CODEX_API_KEY": "provider-secret",
                "CODEX_PROVIDER": 'nebius"]\\nunsafe=true',
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "simple provider identifier"):
                DaytonaSandboxManager()._settings()


if __name__ == "__main__":
    unittest.main()

class BootstrapConfigurationTests(unittest.TestCase):
    def test_bootstrap_writes_the_selected_provider_to_codex_config(self) -> None:
        captured: dict[str, str] = {}

        class Process:
            def code_run(self, code: str):
                captured["code"] = code
                return type("Response", (), {"exit_code": 0, "result": "ok"})()

        with patch.dict(
            os.environ,
            {
                "DAYTONA_API_KEY": "daytona-secret",
                "CODEX_API_KEY": "provider-secret",
                "CODEX_PROVIDER": "nebius",
                "CODEX_PROVIDER_NAME": "Nebius AI Studio",
                "CODEX_BASE_URL": "https://provider.example/v1/",
            },
            clear=True,
        ):
            manager = DaytonaSandboxManager()
            manager.sandbox = type("Sandbox", (), {"process": Process()})()
            manager.bootstrap()

        self.assertIn("model_provider = ' + \"nebius\"", captured["code"])
        self.assertIn('[model_providers." + "nebius" + "]', captured["code"])
        self.assertIn("name = ' + \"Nebius AI Studio\"", captured["code"])
