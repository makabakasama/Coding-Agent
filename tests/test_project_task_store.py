"""Unified ordinary rounds with legacy compatibility; no real model."""
import json
import unittest

from repo_agent.backend.repositories.project_task_store import ProjectTaskStore
from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.repositories.task_store import TaskStore
from tests import test_tasks as fixtures
from tests.test_task_execution import ScriptedModel


class ProjectRoundsTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def service_for(self):
        return TaskService(ProjectTaskStore(self.data, self.root / 'runs', self.root / 'experiments'),
                           TaskProjects(self.config), workspace_root=self.root / 'legacy-workspaces',
                           project_root=self.root, model_factory=ScriptedModel)

    def test_new_round_budget_override_preserves_previous_contracts(self):
        from repo_agent.backend.schemas.task_contract import TaskError
        service = self.service_for()
        original = service.create(fixtures.request_contract(allowTests=False, checkTargets=[], maxSteps=12))
        key = service.organization(original['taskId'])['taskKey']
        updated = service.new_round('sample', key, {'maxSteps': 50})
        self.assertEqual(50, updated['contract']['maxSteps'])
        self.assertEqual(12, service.get(original['taskId'])['contract']['maxSteps'])
        self.assertEqual(12, service.ordinary_task('sample', key)['contract']['maxSteps'])
        for budget in [0, 51, True, 1.5, '50']:
            with self.subTest(budget=budget), self.assertRaises(TaskError):
                service.new_round('sample', key, {'maxSteps': budget})
        self.assertEqual(2, len(service.store.rounds('sample', key)))

    def test_two_rounds_restart_execution_and_legacy_preservation(self):
        legacy = self.service.create(fixtures.request_contract(allowTests=False, checkTargets=[]))
        legacy_path = self.data / ('task_' + legacy['taskId']) / 'task.json'
        before = legacy_path.read_bytes()
        service = self.service_for()
        first = service.create(fixtures.request_contract(allowTests=False, checkTargets=[]))
        info = service.organization(first['taskId'])
        key = info['taskKey']
        first_dir = service.store.directory(first['taskId'])
        self.assertEqual(self.root / 'runs' / key / ('run_' + info['round_id']), first_dir)
        snapshot = (first_dir / 'task.json').read_bytes()
        (self.repo / 'calc.py').write_text('value = 2\n', encoding='utf-8')
        self.git('add', '.')
        self.git('-c', 'user.name=Offline', '-c', 'user.email=offline@example.invalid', 'commit', '-qm', 'second')
        with fixtures.serving(service) as port:
            status, _, second = fixtures.http(port, 'POST', f'/api/projects/sample/tasks/{key}/rounds', {})
            self.assertEqual(201, status)
            self.assertEqual(2, len(fixtures.http(port, 'GET', f'/api/projects/sample/tasks/{key}/rounds')[2]['rounds']))
            self.assertEqual(1, len(fixtures.http(port, 'GET', '/api/projects/sample/tasks')[2]['tasks']))
        self.assertNotEqual(first['taskId'], second['taskId'])
        self.assertNotEqual(first['contract']['baseCommit'], second['contract']['baseCommit'])
        for record in (first, second):
            task_id = record['taskId']
            self.assertEqual('prepared', service.prepare(task_id, {})['status'])
            identity = json.loads((service.store.directory(task_id) / 'preparation/identity.json').read_text())
            self.assertTrue(identity['workspace'].replace('\\', '/').startswith((self.root / 'experiments/sample/workspaces').as_posix()))
            service.execution.start(task_id, {})
            for thread in service.execution.threads:
                thread.join(20)
                self.assertFalse(thread.is_alive())
            self.assertEqual('passed', service.execution.result(task_id)['review']['status'])
        restart = self.service_for()
        self.assertEqual(first, restart.get(first['taskId']))
        self.assertEqual('terminated', restart.execution.get(second['taskId'])['status'])
        self.assertEqual(3, len(restart.list()))
        self.assertEqual(legacy, restart.get(legacy['taskId']))
        self.assertEqual(before, legacy_path.read_bytes())
        self.assertEqual(snapshot, (first_dir / 'task.json').read_bytes())


if __name__ == '__main__':
    unittest.main()


class RequirementOrderingTest(unittest.TestCase):
    def test_project_requirements_are_newest_first_regardless_of_storage_order(self):
        from types import SimpleNamespace
        values = [
            {'taskKey':'older','createdAt':'2026-11-02T09:00:00Z'},
            {'taskKey':'maxsteps','createdAt':'2026-11-09T10:56:38+08:00'},
            {'taskKey':'middle','createdAt':'2026-11-03T09:00:00Z'},
        ]
        service = TaskService.__new__(TaskService)
        service.projects = SimpleNamespace(list=lambda:[{'projectId':'sample'}])
        service.store = SimpleNamespace(requirements=lambda project:values, rounds=lambda project,key:[])
        result = service.ordinary_tasks('sample')
        self.assertEqual(['maxsteps','middle','older'],[v['taskKey'] for v in result['tasks']])
