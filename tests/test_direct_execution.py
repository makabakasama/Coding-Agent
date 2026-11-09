import json
from pathlib import Path
import uuid
import unittest
from unittest.mock import patch

from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.repositories.task_store import TaskStore
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.core.decisions import Decision
from tests import test_tasks as fixtures
from tests.test_task_execution import ScriptedModel


class DirectExecutionTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def run_direct(self, decisions, allowed_paths):
        service = TaskService(self.service.store, TaskProjects(self.config),
                              workspace_root=self.root/'workspaces', model_factory=lambda:ScriptedModel(decisions))
        record = service.create(fixtures.request_contract(workspaceMode='direct', allowedPaths=allowed_paths,
                                                          allowTests=False, checkTargets=[]))
        state = service.execution.run_claimed(record['taskId'], uuid.uuid4().hex)
        self.assertEqual('agent_reported_finished',service.execution.result(record['taskId'])['stopReason'])
        return service, record['taskId'], {'executionId':state['executionId']}

    def test_undo_restores_deleted_file_and_removes_new_file(self):
        (self.repo/'calc.py').write_bytes(b'value = 1\n')
        patch_text = ('--- a/calc.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-value = 1\n'
                      '--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+created = True\n')
        service,task_id,request = self.run_direct([
            Decision('apply_patch',{'patch':patch_text}),Decision('finish',{'answer':'changed'})],['calc.py','new.py'])
        self.assertFalse((self.repo/'calc.py').exists())
        self.assertTrue((self.repo/'new.py').exists())
        self.assertEqual('undone',service.undo.apply(task_id,request)['status'])
        self.assertEqual('value = 1\n',(self.repo/'calc.py').read_text())
        self.assertFalse((self.repo/'new.py').exists())

    def test_partial_undo_can_resume_without_losing_the_original_run(self):
        (self.repo/'second.py').write_text('other = 1\n',encoding='utf-8')
        service,task_id,request = self.run_direct([
            Decision('replace_text',{'path':'calc.py','old_text':'value = 1','new_text':'value = 2'}),
            Decision('replace_text',{'path':'second.py','old_text':'other = 1','new_text':'other = 2'}),
            Decision('finish',{'answer':'changed'})],['calc.py','second.py'])
        from repo_agent.infrastructure.file_io import write_bytes
        def fail_second(path,raw):
            if path.name=='second.py':
                raise PermissionError('temporary fixture failure')
            write_bytes(path,raw)
        with patch('repo_agent.backend.services.task_undo.write_bytes',side_effect=fail_second):
            with self.assertRaises(TaskError):
                service.undo.apply(task_id,request)
        self.assertEqual('value = 1\n',(self.repo/'calc.py').read_text())
        self.assertEqual('other = 2\n',(self.repo/'second.py').read_text())
        self.assertTrue(service.undo.get(task_id)['canUndo'])
        self.assertEqual('undone',service.undo.apply(task_id,request)['status'])
        self.assertEqual('other = 1\n',(self.repo/'second.py').read_text())
        self.assertEqual('agent_reported_finished',service.execution.result(task_id)['stopReason'])

    def test_direct_task_edits_source_and_exports_only_this_run(self):
        (self.repo/'.env.example').write_text('API_KEY=\n', encoding='utf-8')
        self.git('add', '.env.example')
        (self.repo/'calc.py').write_text('value = 1\nuser_change = True\n', encoding='utf-8')
        (self.repo/'.gitignore').write_text('runs/\n', encoding='utf-8')
        (self.repo/'runs').mkdir()
        (self.repo/'runs/private.json').write_text('{"secret":true}', encoding='utf-8')
        decisions = [Decision('replace_text', {'path':'calc.py','old_text':'value = 1','new_text':'value = 2'}),
                     Decision('finish', {'answer':'Changed current source'})]
        service = TaskService(TaskStore(self.repo/'runs/tasks'), TaskProjects(self.config),
                              workspace_root=self.root/'workspaces', model_factory=lambda:ScriptedModel(decisions))
        before = self.git('worktree','list','--porcelain')
        record = service.create(fixtures.request_contract(workspaceMode='direct', allowTests=False, checkTargets=[]))
        task_id = record['taskId']
        self.assertFalse((service.store.directory(task_id)/'preparation').exists())
        self.assertEqual(self.repo.resolve(), service.execution._workspace(record)[0])
        self.assertEqual(before, self.git('worktree','list','--porcelain'))
        state = service.execution.run_claimed(task_id, uuid.uuid4().hex)
        identity = json.loads((service.store.directory(task_id)/'preparation/identity.json').read_text())
        self.assertEqual(self.repo.resolve(), Path(identity['workspace']))
        self.assertEqual('terminated', state['status'])
        result = service.execution.result(task_id)
        self.assertEqual('agent_reported_finished', result['stopReason'])
        self.assertEqual('passed', result['review']['status'])
        self.assertEqual('value = 2\nuser_change = True\n', (self.repo/'calc.py').read_text())
        patch = (service.store.directory(task_id)/'execution/candidate.patch').read_text()
        self.assertIn('-value = 1', patch)
        self.assertIn('+value = 2', patch)
        self.assertNotIn('+user_change = True', patch)
        self.assertEqual(['calc.py'], result['review']['changedPaths'])
        baseline = service.store.directory(task_id)/'execution/baseline/files/calc.py'
        self.assertEqual('value = 1\nuser_change = True\n', baseline.read_text())
        self.assertTrue(service.undo.get(task_id)['canUndo'])
        (self.repo/'calc.py').write_text('later user edit\n',encoding='utf-8')
        status = service.undo.get(task_id)
        self.assertEqual(['calc.py'],status['conflicts'])
        with self.assertRaises(TaskError):
            service.undo.apply(task_id,{'executionId':state['executionId']})
        self.assertEqual('later user edit\n',(self.repo/'calc.py').read_text())
        (self.repo/'calc.py').write_text('value = 2\nuser_change = True\n',encoding='utf-8')
        with fixtures.serving(service) as port:
            response = fixtures.http(port,'GET',f'/api/tasks/{task_id}/execution/undo')
            self.assertEqual(200,response[0])
            self.assertTrue(response[2]['canUndo'])
            self.assertEqual(400,fixtures.http(port,'POST',f'/api/tasks/{task_id}/execution/undo',{})[0])
            response = fixtures.http(port,'POST',f'/api/tasks/{task_id}/execution/undo',{'executionId':state['executionId']})
            self.assertEqual(200,response[0])
            undo = response[2]
        self.assertEqual('undone',undo['status'])
        self.assertEqual('value = 1\nuser_change = True\n',(self.repo/'calc.py').read_text())
        self.assertEqual('undone',service.undo.apply(task_id,{'executionId':state['executionId']})['status'])

    def test_direct_read_only_task_preserves_dirty_source(self):
        (self.repo/'calc.py').write_text('existing = 2\n', encoding='utf-8')
        service = TaskService(self.service.store, TaskProjects(self.config),
                              workspace_root=self.root/'workspaces', model_factory=ScriptedModel)
        record = service.create(fixtures.request_contract(workspaceMode='direct', kind='read_code',
                                allowedPaths=[], allowTests=False, checkTargets=[]))
        service.execution.run_claimed(record['taskId'], uuid.uuid4().hex)
        self.assertEqual('existing = 2\n', (self.repo/'calc.py').read_text())
        self.assertEqual([],service.execution.result(record['taskId'])['review']['changedPaths'])
