"""Offline launcher regressions. Image resolution is covered in test_image_provider."""
from pathlib import Path
import tempfile
import unittest

from repo_agent.evaluation.official_runner import linux_text_artifacts


class OfficialRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_lf_wrapper_is_scoped_and_restored_after_error(self):
        original = Path.write_text
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with linux_text_artifacts(windows=True):
                for name in ('eval.sh', 'patch.diff'):
                    (self.root / name).write_text('中文\nline\n', newline='\r\n')
                    self.assertEqual('中文\nline\n'.encode('utf-8'), (self.root / name).read_bytes())
                (self.root / 'other.txt').write_text('line\n', newline='\r\n')
                self.assertEqual(b'line\r\n', (self.root / 'other.txt').read_bytes())
                raise RuntimeError('fixture')
        self.assertIs(original, Path.write_text)


if __name__ == '__main__':
    unittest.main()
