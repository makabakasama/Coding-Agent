import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.config import read_dotenv
from repo_agent.provider import OpenAICompatibleProvider


class ConfigTest(unittest.TestCase):
    def test_profiles_switch_as_a_group_without_legacy_fallback(self):
        local = {"LLM_PROFILE": "gpt", "LLM_MODEL": "legacy",
                 "LLM_BASE_URL": "https://legacy.invalid", "LLM_API_KEY": "legacy-key"}
        for name in ("gpt", "qwen"):
            local.update({f"{name.upper()}_MODEL": name,
                          f"{name.upper()}_BASE_URL": f"https://{name}.invalid/v1",
                          f"{name.upper()}_API_KEY": f"{name}-key"})
        with patch("repo_agent.provider.read_dotenv", return_value=local), patch.dict(os.environ, {}, clear=True):
            for name in ("gpt", "qwen"):
                with patch.dict(os.environ, {"LLM_PROFILE": name, "OPENAI_API_KEY": "unrelated"}):
                    p = OpenAICompatibleProvider()
                    self.assertEqual((name, f"https://{name}.invalid/v1", f"{name}-key"),
                                     (p.model, p.base_url, p.api_key))
            del local["GPT_API_KEY"]
            self.assertEqual("", OpenAICompatibleProvider().api_key)
            del local["GPT_BASE_URL"]
            with self.assertRaisesRegex(RuntimeError, "selected profile"):
                OpenAICompatibleProvider()
            local["LLM_PROFILE"] = "typo"
            with self.assertRaisesRegex(RuntimeError, "LLM_PROFILE"):
                OpenAICompatibleProvider()

    def test_llm_names_and_legacy_precedence(self):
        local = {"LLM_PROVIDER": "openai_compatible", "LLM_MODEL": "new-model",
                 "LLM_BASE_URL": "https://example.invalid/v1/", "LLM_API_KEY": "new-key",
                 "OPENAI_MODEL": "old-model", "OPENAI_API_KEY": "old-key"}
        with patch("repo_agent.provider.read_dotenv", return_value=local), patch.dict(os.environ, {}, clear=True):
            provider = OpenAICompatibleProvider()
            self.assertEqual(("new-model", "https://example.invalid/v1", "new-key"),
                             (provider.model, provider.base_url, provider.api_key))
            with patch.dict(os.environ, {"OPENAI_MODEL": "env-model"}):
                self.assertEqual("env-model", OpenAICompatibleProvider().model)
            local["LLM_PROVIDER"] = "unsupported"
            with self.assertRaisesRegex(RuntimeError, "LLM_PROVIDER"):
                OpenAICompatibleProvider()

    def test_literal_values_and_safe_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            self.assertEqual({}, read_dotenv(path))
            path.write_text(
                '# comment\nOPENAI_MODEL="example"\nDASHSCOPE_API_KEY=literal#=$TOKEN\n',
                encoding="utf-8-sig")
            self.assertEqual({"OPENAI_MODEL": "example", "DASHSCOPE_API_KEY": "literal#=$TOKEN"},
                             read_dotenv(path))
            path.write_text('DASHSCOPE_API_KEY="fake-secret\n', encoding="utf-8")
            with self.assertRaises(ValueError) as error:
                read_dotenv(path)
            self.assertNotIn("fake-secret", str(error.exception))

    def test_provider_configuration_precedence(self):
        local = {"OPENAI_MODEL": "file-model", "OPENAI_BASE_URL": "https://file.invalid/v1/",
                 "OPENAI_API_KEY": "file-key"}
        with patch("repo_agent.provider.read_dotenv", return_value=local), patch.dict(os.environ, {}, clear=True):
            provider = OpenAICompatibleProvider()
            self.assertEqual("file-model", provider.model)
            self.assertEqual("https://file.invalid/v1", provider.base_url)
            self.assertEqual("file-key", provider.api_key)
            self.assertNotIn("OPENAI_API_KEY", os.environ)
            with patch.dict(os.environ, {"OPENAI_MODEL": "env-model", "DASHSCOPE_API_KEY": "env-key"}):
                provider = OpenAICompatibleProvider()
                self.assertEqual("env-model", provider.model)
                self.assertEqual("env-key", provider.api_key)
                explicit = OpenAICompatibleProvider(model="explicit", base_url="https://explicit.invalid/v1", api_key="")
                self.assertEqual("explicit", explicit.model)
                self.assertEqual("https://explicit.invalid/v1", explicit.base_url)
                self.assertEqual("", explicit.api_key)
