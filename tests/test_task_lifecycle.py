"""Recoverable task deletion through local HTTP, with no real model calls."""
import unittest
from unittest.mock import patch

from repo_agent.backend.repositories.project_task_store import ProjectTaskStore
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.services.task_service import TaskService
from repo_agent.infrastructure.file_io import exclusive
from tests import test_tasks as fixtures
from tests.test_task_execution import ScriptedModel


class TaskLifecycleTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def service_for(self):
        return TaskService(ProjectTaskStore(self.data, self.root / 'runs', self.root / 'workspaces'),
                           TaskProjects(self.config), project_root=self.root, model_factory=ScriptedModel)

    def test_single_task_status_query_does_not_load_execution_or_full_list(self):
        service = self.service_for()
        task_id = service.create(fixtures.request_contract())['taskId']
        with fixtures.serving(service) as port:
            endpoint = f'/api/tasks/{task_id}/lifecycle'
            with patch.object(service.execution, 'get', side_effect=AssertionError('Execution read forbidden')):
                status, _, result = fixtures.http(port, 'GET', endpoint)
            self.assertEqual(200, status)
            self.assertEqual({'taskId': task_id, 'deleted': False}, result)
            service.lifecycle.change(task_id, {'deleted': True})
            self.assertEqual({'taskId': task_id, 'deleted': True}, fixtures.http(port, 'GET', endpoint)[2])
            self.assertEqual(400, fixtures.http(port, 'GET', '/api/tasks/invalid/lifecycle')[0])

    def test_delete_restore_and_restart_preserve_materials_and_project_history(self):
        service = self.service_for()
        record = service.create(fixtures.request_contract())
        task_id = record['taskId']
        info = service.organization(task_id)
        directory = service.store.directory(task_id)
        before = (directory / 'task.json').read_bytes()
        with fixtures.serving(service) as port:
            endpoint = f'/api/tasks/{task_id}/lifecycle'
            for _ in range(2):
                self.assertEqual(200, fixtures.http(port, 'POST', endpoint, {'deleted': True})[0])
            self.assertEqual([], fixtures.http(port, 'GET', '/api/tasks')[2]['tasks'])
            self.assertEqual(task_id, fixtures.http(port, 'GET', '/api/trash')[2]['tasks'][0]['taskId'])
            self.assertEqual([], service.store.rounds('sample', info['taskKey']))
            self.assertEqual([], service.ordinary_tasks('sample')['tasks'])
            self.assertEqual(409, fixtures.http(port, 'POST', f'/api/tasks/{task_id}/preparation', {})[0])
            self.assertEqual(409, fixtures.http(port, 'POST', f'/api/tasks/{task_id}/execution', {})[0])
            self.assertEqual(before, (directory / 'task.json').read_bytes())
        restarted = self.service_for()
        self.assertEqual([], restarted.list())
        self.assertEqual(task_id, restarted.list(deleted=True)[0]['taskId'])
        restarted.lifecycle.change(task_id, {'deleted': False})
        self.assertEqual(task_id, restarted.list()[0]['taskId'])
        self.assertEqual(1, len(restarted.store.rounds('sample', info['taskKey'])))
        self.assertEqual(before, (directory / 'task.json').read_bytes())

    def test_delete_one_round_preserves_other_round_and_active_preparation_is_rejected(self):
        service = self.service_for()
        first = service.create(fixtures.request_contract())
        task_id = first['taskId']
        key = service.organization(task_id)['taskKey']
        second = service.new_round('sample', key, {})
        with exclusive(service.store.directory(task_id) / 'preparation.lock'):
            with self.assertRaises(fixtures.TaskError) as error:
                service.lifecycle.change(task_id, {'deleted': True})
            self.assertEqual('TASK_BUSY', error.exception.code)
        service.lifecycle.change(task_id, {'deleted': True})
        self.assertEqual([second['taskId']], [r['runTaskId'] for r in service.store.rounds('sample', key)])
        self.assertEqual(1, service.ordinary_tasks('sample')['tasks'][0]['roundCount'])

    def test_invalid_payload_and_cross_origin_write_are_rejected(self):
        service = self.service_for()
        task_id = service.create(fixtures.request_contract())['taskId']
        with fixtures.serving(service) as port:
            endpoint = f'/api/tasks/{task_id}/lifecycle'
            for value in ({}, {'deleted': 'true'}, {'deleted': True, 'other': 1}):
                self.assertEqual(400, fixtures.http(port, 'POST', endpoint, value)[0])
            self.assertEqual(403, fixtures.http(port, 'POST', endpoint, {'deleted': True},
                                               {'Origin': 'http://external.invalid'})[0])
            self.assertEqual(400, fixtures.http(port, 'POST', '/api/tasks/invalid/lifecycle', {'deleted': True})[0])
