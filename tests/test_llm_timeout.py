"""Timeout policy tests without external APIs, real keys or model dependencies."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.clip_selector import CLIP_SCHEMA
from core.config import load_config
from core.llm_client import HTTPStatusError, LLMClient, LLMError, LLMTimeoutError


def response():
    return {"choices": [{"message": {"content": '{"clips":[]}'}}]}


class TimeoutPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_new_window_restores_base_timeout_after_previous_window_retry(self):
        timeouts = []
        client = None

        def transport(payload):
            timeouts.append(client._request_timeout)
            if len(timeouts) == 1:
                raise LLMTimeoutError(client._request_timeout, 0.15, "waiting_response_headers")
            return response()

        client = LLMClient({"timeout": 0.15, "max_timeout": 0.6, "max_retries": 2},
                           self.root / "budget.json", transport, sleeper=lambda _: None)
        self.assertEqual(client.complete([], CLIP_SCHEMA), {"clips": []})
        self.assertEqual(client.complete([], CLIP_SCHEMA), {"clips": []})
        self.assertEqual(timeouts, [0.15, 0.3, 0.15])

    def test_timeout_growth_is_capped_even_with_more_retries(self):
        timeouts = []
        client = None

        def transport(payload):
            timeouts.append(client._request_timeout)
            raise LLMTimeoutError(client._request_timeout, 0.1, "reading_response_body")

        client = LLMClient({"timeout": 0.1, "max_timeout": 0.25, "max_retries": 4},
                           self.root / "budget.json", transport, sleeper=lambda _: None)
        with self.assertRaises(LLMError):
            client.complete([], CLIP_SCHEMA)
        self.assertEqual(timeouts, [0.1, 0.2, 0.25, 0.25, 0.25])

    def test_format_and_validation_retry_do_not_increase_timeout(self):
        timeouts = []
        client = None

        def transport(payload):
            timeouts.append(client._request_timeout)
            if len(timeouts) == 1:
                raise HTTPStatusError(400, "unsupported response_format")
            if len(timeouts) == 2:
                return {"choices": [{"message": {"content": "invalid JSON"}}]}
            return response()

        client = LLMClient({"timeout": 0.15, "max_timeout": 0.6, "max_retries": 1},
                           self.root / "budget.json", transport, sleeper=lambda _: None)
        self.assertEqual(client.complete([], CLIP_SCHEMA), {"clips": []})
        self.assertEqual(timeouts, [0.15, 0.15, 0.15])

    def test_default_timeout_policy_without_explicit_options(self):
        seen = []
        client = None

        def transport(payload):
            seen.append(client._request_timeout)
            if len(seen) < 3:
                raise LLMTimeoutError(client._request_timeout, 0.1, "waiting_response_headers")
            return response()

        client = LLMClient({}, self.root / "budget.json", transport, sleeper=lambda _: None)
        self.assertEqual(client.complete([], CLIP_SCHEMA), {"clips": []})
        self.assertEqual(seen, [180, 360, 600])


class TimeoutConfigurationTests(unittest.TestCase):
    def configuration(self, root, yaml_text="", env=None, dotenv=""):
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "settings.yaml").write_text(yaml_text, encoding="utf-8")
        (root / ".env").write_text(dotenv, encoding="utf-8")
        with patch("core.config.ROOT", root), patch.dict(os.environ, env or {}, clear=True):
            return load_config()

    def test_shipped_default_timeouts_are_180_and_600_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.configuration(Path(tmp))
        self.assertEqual(cfg["openai"]["timeout"], 180)
        self.assertEqual(cfg["openai"]["max_timeout"], 600)

    def test_empty_environment_preserves_yaml_and_numeric_env_overrides_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yaml_text = "openai:\n  timeout: 90\n  max_timeout: 300\n"
            cfg = self.configuration(root, yaml_text,
                                     {"OPENAI_TIMEOUT": "", "OPENAI_MAX_TIMEOUT": "   "})
            self.assertEqual((cfg["openai"]["timeout"], cfg["openai"]["max_timeout"]), (90, 300))
            cfg = self.configuration(root, yaml_text,
                                     {"OPENAI_TIMEOUT": " 120.5 ", "OPENAI_MAX_TIMEOUT": "420"})
            self.assertEqual((cfg["openai"]["timeout"], cfg["openai"]["max_timeout"]), (120.5, 420))

    def test_dotenv_timeout_settings_use_same_numeric_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.configuration(Path(tmp), dotenv="OPENAI_TIMEOUT=100\nOPENAI_MAX_TIMEOUT=350\n")
        self.assertEqual((cfg["openai"]["timeout"], cfg["openai"]["max_timeout"]), (100, 350))

    def test_invalid_timeout_values_fail_configuration_validation(self):
        cases = ["openai:\n  timeout: 0\n", "openai:\n  timeout: -1\n",
                 "openai:\n  timeout: .nan\n", "openai:\n  timeout: .inf\n",
                 "openai:\n  timeout: true\n", "openai:\n  timeout: 300\n  max_timeout: 100\n",
                 "openai:\n  max_timeout: .nan\n", "openai:\n  max_timeout: 0\n"]
        with tempfile.TemporaryDirectory() as tmp:
            for yaml_text in cases:
                with self.subTest(yaml=yaml_text), self.assertRaises(ValueError):
                    self.configuration(Path(tmp), yaml_text)

    def test_invalid_numeric_environment_fails_before_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, value in (("OPENAI_TIMEOUT", "NaN"), ("OPENAI_TIMEOUT", "wrong"),
                                ("OPENAI_TIMEOUT", "0"), ("OPENAI_MAX_TIMEOUT", "inf"),
                                ("OPENAI_MAX_TIMEOUT", "1")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.configuration(Path(tmp), env={name: value})


if __name__ == "__main__":
    unittest.main()
