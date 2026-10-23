import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.config import read_dotenv
from repo_agent.provider import OpenAICompatibleProvider


class ConfigTest(unittest.TestCase):
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
