from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from repo_agent.infrastructure.file_io import read_json, write_json


class AtomicJsonTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'state.json'
        write_json(self.path, {'steps': 7})

    def test_transient_replace_denial_keeps_old_state_until_success(self):
        original = Path.replace
        attempts = []
        def replace(temporary, destination):
            attempts.append(temporary)
            if len(attempts) < 3:
                self.assertEqual({'steps': 7}, read_json(destination))
                raise PermissionError('temporarily open destination')
            return original(temporary, destination)
        with patch.object(Path, 'replace', replace), patch('repo_agent.infrastructure.file_io.time.sleep'):
            write_json(self.path, {'steps': 8})
        self.assertEqual(3, len(attempts))
        self.assertEqual({'steps': 8}, read_json(self.path))
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_permanent_denial_raises_and_keeps_previous_state(self):
        with patch.object(Path, 'replace', side_effect=PermissionError('denied')) as replace:
            with patch('repo_agent.infrastructure.file_io.time.sleep'), self.assertRaises(PermissionError):
                write_json(self.path, {'steps': 8})
        self.assertEqual(8, replace.call_count)
        self.assertEqual({'steps': 7}, read_json(self.path))
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_serialization_failure_does_not_damage_previous_state(self):
        with self.assertRaises(TypeError):
            write_json(self.path, {'unsupported': object()})
        self.assertEqual({'steps': 7}, read_json(self.path))
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_transient_read_denial_recovers_without_changing_saved_state(self):
        original = Path.open
        attempts = []

        def open_record(path, *args, **kwargs):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError('temporarily replacing destination')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'open', open_record), patch('repo_agent.infrastructure.file_io.time.sleep') as sleep:
            self.assertEqual({'steps': 7}, read_json(self.path))
        self.assertEqual(3, len(attempts))
        self.assertEqual(2, sleep.call_count)

    def test_permanent_read_denial_remains_an_error_after_bounded_retries(self):
        with patch.object(Path, 'open', side_effect=PermissionError('denied')) as opener:
            with patch('repo_agent.infrastructure.file_io.time.sleep') as sleep, self.assertRaises(PermissionError):
                read_json(self.path)
        self.assertEqual(8, opener.call_count)
        self.assertEqual(7, sleep.call_count)

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_windows_open_reader_can_overlap_state_update(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.path.open('rb') as reader:
                future = pool.submit(write_json, self.path, {'steps': 8})
                time.sleep(.08)
                self.assertEqual(b'{\n  "steps": 7\n}\n', reader.read())
            future.result(timeout=3)
        self.assertEqual({'steps': 8}, read_json(self.path))
