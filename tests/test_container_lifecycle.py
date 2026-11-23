"""Offline lifecycle regressions. No Docker daemon or model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from repo_agent.evaluation.container_lifecycle import (
    CONTAINER_LIMITS, container_slot, limited_official_containers,
    remove_container, require_idle)


class ContainerLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.not_found = type('NotFound', (Exception,), {})
        modules = patch.dict('sys.modules', {
            'docker.errors': SimpleNamespace(NotFound=self.not_found)})
        modules.start()
        self.addCleanup(modules.stop)

    def test_slot_rejects_overlap_and_releases_after_failure(self):
        with patch('repo_agent.evaluation.container_lifecycle.runtime_root', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'fixture'):
                with container_slot():
                    with self.assertRaisesRegex(RuntimeError, 'container slot'):
                        with container_slot():
                            self.fail('A second slot was acquired')
                    raise ValueError('fixture')
            with container_slot():
                self.assertTrue((self.root / 'evaluation-container.lock').is_file())

    def test_stop_failure_still_removes_and_records_actual_removal(self):
        container = Mock(id='offline')
        container.stop.side_effect = RuntimeError('stop failed')
        remove_container(container, self.root, 'check')
        container.remove.assert_called_once_with(force=True, v=False)
        record = json.loads((self.root / 'check-cleanup.json').read_bytes())
        self.assertEqual('removed', record['status'])
        self.assertEqual('RuntimeError', record['stopError']['errorType'])

    def test_slot_blocks_another_process_then_allows_it_after_release(self):
        script = (
            'import sys\n'
            'from pathlib import Path\n'
            'from repo_agent.evaluation import container_lifecycle as lifecycle\n'
            'lifecycle.runtime_root = lambda: Path(sys.argv[1])\n'
            'try:\n'
            '    with lifecycle.container_slot():\n'
            '        pass\n'
            'except RuntimeError:\n'
            '    sys.exit(2)\n')
        command = [sys.executable, '-c', script, str(self.root)]
        with patch('repo_agent.evaluation.container_lifecycle.runtime_root', return_value=self.root):
            with container_slot():
                blocked = subprocess.run(command, capture_output=True, timeout=15)
            released = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(2, blocked.returncode, blocked.stderr.decode(errors='replace'))
        self.assertEqual(0, released.returncode, released.stderr.decode(errors='replace'))

    def test_remove_failure_is_recorded_and_raised(self):
        container = Mock(id='offline')
        container.remove.side_effect = RuntimeError('remove failed')
        with self.assertRaisesRegex(RuntimeError, 'remove failed'):
            remove_container(container, self.root, 'check')
        self.assertEqual('cleanup_failed', json.loads(
            (self.root / 'check-cleanup.json').read_bytes())['status'])

    def test_already_removed_is_accepted(self):
        container = Mock(id='offline')
        container.remove.side_effect = self.not_found('gone')
        remove_container(container, self.root, 'check')
        self.assertEqual('already_removed', json.loads(
            (self.root / 'check-cleanup.json').read_bytes())['status'])

    def test_orphan_blocks_new_run(self):
        client = Mock()
        client.containers.list.return_value = [SimpleNamespace(
            name='sweb.eval.task.old', labels={})]
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            require_idle(client)

    def test_official_limits_precede_creation_and_wrapper_restores_after_error(self):
        container = Mock(id='official')
        create = Mock(return_value=container)
        collection_class = type('ContainerCollection', (), {'create': create})
        with patch.dict('sys.modules', {'docker.models.containers': SimpleNamespace(
                ContainerCollection=collection_class)}):
            with self.assertRaisesRegex(ValueError, 'fixture'):
                with limited_official_containers(self.root, 'Task'):
                    collection_class.create(Mock(), image='image', name='sweb.eval.task.run')
                    for key, value in CONTAINER_LIMITS.items():
                        self.assertEqual(value, create.call_args.kwargs[key])
                    with self.assertRaisesRegex(RuntimeError, 'Only one'):
                        collection_class.create(Mock(), image='image', name='sweb.eval.task.other')
                    raise ValueError('fixture')
            self.assertIs(create, collection_class.create)
        container.remove.assert_called_once_with(force=True, v=False)
