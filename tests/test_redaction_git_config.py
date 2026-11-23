"""Git config labels are public; real credential values remain protected."""
import unittest
from unittest.mock import patch

from repo_agent.infrastructure.redaction import Redactor


class GitConfigRedactionTests(unittest.TestCase):
    def test_git_key_label_is_not_a_secret_but_api_token_is(self):
        with patch.dict('os.environ', {'GIT_CONFIG_KEY_0': 'core.longpaths'}, clear=True), \
                patch('repo_agent.infrastructure.redaction.read_dotenv',
                      return_value={'LLM_API_KEY': 'fixture-private-token'}):
            redactor = Redactor()
        self.assertEqual('git -c core.longpaths=true', redactor.private('git -c core.longpaths=true'))
        self.assertEqual('[REDACTED]', redactor.private('fixture-private-token'))

    def test_sensitive_git_value_is_protected(self):
        with patch.dict('os.environ', {'GIT_CONFIG_KEY_0': 'http.extraheader',
                                      'GIT_CONFIG_VALUE_0': 'Authorization: fixture-private-value'}, clear=True), \
                patch('repo_agent.infrastructure.redaction.read_dotenv', return_value={}):
            redactor = Redactor()
        self.assertEqual('http.extraheader', redactor.private('http.extraheader'))
        self.assertEqual('[REDACTED]', redactor.private('Authorization: fixture-private-value'))
