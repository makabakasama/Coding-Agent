import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_agent.core.tools import RepoTools
from repo_agent.evaluation.check_runner import check_result, test_command as command


class P0Acceptance(unittest.TestCase):
    def test_django_target_mapping_and_results(self):
        config = {'temporary': False, 'repo': 'django/django',
                  'target': 'tests/forms_tests/tests/test_media.py'}
        self.assertEqual('forms_tests.tests.test_media', command(config)[-1])
        self.assertIn('tests/runtests.py', command(config))
        self.assertTrue(check_result(config, 0, 'Ran 2 tests in 0.1s\nOK\n')['passed'])
        self.assertFalse(check_result(config, 1, 'Ran 2 tests in 0.1s\nFAILED (failures=1)\n')['passed'])

    def test_large_file_edit_preserves_crlf_and_detects_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'large.py'
            file.write_bytes(b'# filler\r\n' * 100000 + b'value = 1\r\n')
            tool = RepoTools(Path(root), allow_writes=True)
            observation = tool.read_file('large.py', 100001, 100001)
            self.assertIsNotNone(observation['snapshot_id'])
            result = tool.replace_lines(observation['snapshot_id'], 100001, 100001, 'value = 2')
            self.assertTrue(result['applied'])
            self.assertTrue(file.read_bytes().endswith(b'value = 2\r\n'))
            observation = tool.read_file('large.py', 100001, 100001)
            with file.open('ab') as stream:
                stream.write(b'# changed\r\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                tool.replace_lines(observation['snapshot_id'], 100001, 100001, 'value = 3')

    def test_python_syntax_feedback_is_advisory(self):
        with tempfile.TemporaryDirectory() as root:
            tool = RepoTools(Path(root), allow_writes=True)
            result = tool.create_file('broken.py', 'def bad(:\n')
            self.assertTrue(result['applied'])
            self.assertEqual('failed', result['syntax_check']['status'])
            self.assertTrue(result['syntax_check']['advisory'])

    def test_large_file_change_during_edit_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'large.py'
            file.write_bytes(b'# filler\n' * 110000 + b'value = 1\n')
            tool = RepoTools(Path(root), allow_writes=True)
            observation = tool.read_file('large.py', 110001, 110001)
            def concurrent_change(_):
                with file.open('ab') as stream:
                    stream.write(b'# concurrent\n')
            with patch('repo_agent.core.tools.os.fsync', side_effect=concurrent_change):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    tool.replace_lines(observation['snapshot_id'], 110001, 110001, 'value = 2')
            self.assertTrue(file.read_bytes().endswith(b'value = 1\n# concurrent\n'))
            self.assertEqual([], list(Path(root).glob('.agent-edit-*')))

    def test_failed_windows_replacement_keeps_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'large.py'
            original = b'# filler\n' * 110000 + b'value = 1\n'
            file.write_bytes(original)
            tool = RepoTools(Path(root), allow_writes=True)
            observation = tool.read_file('large.py', 110001, 110001)
            with patch('repo_agent.core.tools.os.replace', side_effect=PermissionError('file in use')):
                with self.assertRaises(PermissionError):
                    tool.replace_lines(observation['snapshot_id'], 110001, 110001, 'value = 2')
            self.assertEqual(original, file.read_bytes())
            self.assertEqual([], list(Path(root).glob('.agent-edit-*')))
