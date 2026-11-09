"""Permanent task cleanup uses synthetic repositories and temporary materials."""
from pathlib import Path
import unittest
import tempfile
import uuid
from unittest.mock import patch

from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.infrastructure.file_io import read_json, write_json
from tests import test_task_lifecycle as lifecycle_fixtures
from tests import test_tasks as fixtures


class TaskPurgeTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git
    service_for = lifecycle_fixtures.TaskLifecycleTest.service_for

    def test_confirmation_and_trash_required_then_removes_materials_and_preserves_source(self):
        service = self.service_for()
        task_id = service.create(fixtures.request_contract(workspaceMode='direct'))['taskId']
        directory = service.store.directory(task_id)
        target = self.repo/'calc.py'
        target.write_text('user changes stay here\n', encoding='utf-8')
        with fixtures.serving(service) as port:
            endpoint = f'/api/tasks/{task_id}/purge'
            self.assertEqual(400, fixtures.http(port, 'POST', endpoint, {})[0])
            self.assertEqual(409, fixtures.http(port, 'POST', endpoint, {'confirmTaskId': task_id})[0])
            service.lifecycle.change(task_id, {'deleted': True})
            status, _, result = fixtures.http(port, 'POST', endpoint, {'confirmTaskId': task_id})
            self.assertEqual(200, status)
            self.assertTrue(result['purged'])
            self.assertFalse(directory.exists())
            self.assertEqual('user changes stay here\n', target.read_text(encoding='utf-8'))
            self.assertEqual([], service.list(deleted=True))
            self.assertEqual(410, fixtures.http(port, 'GET', f'/api/tasks/{task_id}')[0])
            self.assertEqual(200, fixtures.http(port, 'POST', endpoint, {'confirmTaskId': task_id})[0])
            self.assertTrue(fixtures.http(port, 'GET', endpoint)[2]['purged'])

    def test_isolated_workspace_is_removed_while_another_round_is_preserved(self):
        service = self.service_for()
        first = service.create(fixtures.request_contract())
        task_id = first['taskId']
        key = service.organization(task_id)['taskKey']
        other = service.new_round('sample', key, {})
        service.prepare(task_id, {})
        directory = service.store.directory(task_id)
        workspace = Path(read_json(directory/'preparation/identity.json')['workspace'])
        self.assertTrue(workspace.exists())
        service.lifecycle.change(task_id, {'deleted': True})
        service.purge.apply(task_id, {'confirmTaskId': task_id})
        self.assertFalse(workspace.exists())
        self.assertTrue(service.store.directory(other['taskId']).exists())
        self.assertEqual(other['taskId'], service.store.rounds('sample', key)[0]['runTaskId'])
        self.assertEqual(1, service.ordinary_tasks('sample')['tasks'][0]['roundCount'])

    def test_failed_cleanup_is_visible_retryable_and_blocks_restoration(self):
        service = self.service_for()
        task_id = service.create(fixtures.request_contract())['taskId']
        service.lifecycle.change(task_id, {'deleted': True})
        with patch('repo_agent.backend.services.task_purge.shutil.rmtree', side_effect=PermissionError('busy')):
            with self.assertRaises(TaskError) as error:
                service.purge.apply(task_id, {'confirmTaskId': task_id})
        self.assertEqual('PURGE_INCOMPLETE', error.exception.code)
        self.assertEqual('purging', service.purge.get(task_id)['status'])
        self.assertTrue(service.list(deleted=True)[0]['purgePending'])
        with self.assertRaises(TaskError):
            service.lifecycle.change(task_id, {'deleted': False})
        service = self.service_for()
        self.assertTrue(service.purge.apply(task_id, {'confirmTaskId': task_id})['purged'])
        self.assertNotIn('record', read_json(service.store.purge_path(task_id)))

    def test_interruption_after_record_removal_can_be_retried(self):
        service = self.service_for()
        task_id = service.create(fixtures.request_contract())['taskId']
        service.lifecycle.change(task_id, {'deleted': True})
        original = service.store.purge_run
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('Interrupted after removal')
        with patch.object(service.store, 'purge_run', side_effect=interrupted):
            with self.assertRaises(TaskError):
                service.purge.apply(task_id, {'confirmTaskId': task_id})
        self.assertTrue(service.purge.apply(task_id, {'confirmTaskId': task_id})['purged'])

    def test_external_logs_are_limited_to_the_recorded_execution_directory(self):
        service = self.service_for()
        task_id = service.create(fixtures.request_contract(workspaceMode='direct'))['taskId']
        service.lifecycle.change(task_id, {'deleted': True})
        directory = service.store.directory(task_id)/'execution'
        directory.mkdir()
        execution_id = uuid.uuid4().hex
        logs = Path(tempfile.gettempdir()).resolve()/'coding-agent-test-logs'/execution_id
        logs.mkdir(parents=True)
        self.addCleanup(lambda: logs.rmdir() if logs.exists() else None)
        unrelated = self.root/'unrelated-logs'
        unrelated.mkdir()
        write_json(directory/'test-log-location.json', {'directory': str(unrelated)})
        state = {'executionId': execution_id, 'status': 'terminated'}
        with patch.object(service.execution, '_read_state', return_value=state):
            with self.assertRaises(TaskError) as error:
                service.purge.apply(task_id, {'confirmTaskId': task_id})
            self.assertEqual('PURGE_PATH_UNSAFE', error.exception.code)
            self.assertTrue(unrelated.exists())
            write_json(directory/'test-log-location.json', {'directory': str(logs)})
            service.purge.apply(task_id, {'confirmTaskId': task_id})
        self.assertFalse(logs.exists())
        self.assertTrue(unrelated.exists())
