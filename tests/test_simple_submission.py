import uuid
import unittest

from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.core.decisions import Decision
from tests import test_tasks as fixtures
from tests.test_task_execution import ScriptedModel


class SimpleSubmissionTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def test_existing_project_needs_only_description(self):
        record=self.service.create({'projectId':'sample','instruction':'Explain this project','kind':'read_code'})
        self.assertEqual([],record['contract']['allowedPaths'])
        self.assertEqual('direct',record['contract']['workspaceMode'])
        self.assertFalse(record['contract']['allowTests'])
        self.assertEqual(50,record['contract']['maxSteps'])

    def test_arbitrary_file_without_git_is_modified_and_can_be_undone(self):
        folder=self.root/'plain-files';folder.mkdir()
        target=folder/'script.py';target.write_text('value = 1\n',encoding='utf-8')
        other=folder/'private.py';other.write_text('private = True\n',encoding='utf-8')
        service=TaskService(self.service.store,TaskProjects(self.config),workspace_root=self.root/'workspaces',
            model_factory=lambda:ScriptedModel([
                Decision('replace_text',{'path':'script.py','old_text':'value = 1','new_text':'value = 2'}),
                Decision('finish',{'answer':'changed'})]))
        record=service.create({'targetPath':str(target),'instruction':'Change the value to 2'})
        self.assertIsNone(record['contract']['baseCommit'])
        self.assertEqual('directory',record['contract']['sourceKind'])
        self.assertEqual(['script.py'],record['contract']['allowedPaths'])
        task_id=record['taskId']
        state=service.execution.run_claimed(task_id,uuid.uuid4().hex)
        result=service.execution.result(task_id)
        self.assertEqual('agent_reported_finished',result['stopReason'])
        self.assertEqual('value = 2\n',target.read_text())
        self.assertFalse((folder/'.git').exists())
        service.undo.apply(task_id,{'executionId':state['executionId']})
        self.assertEqual('value = 1\n',target.read_text())
        from repo_agent.backend.services.task_execution_tools import TaskRepoTools
        tools=TaskRepoTools(folder,contract=record['contract'])
        self.assertEqual(['script.py'],tools.list_files()['files'])
        with self.assertRaises(ValueError):
            tools.read_file('private.py')
        self.assertTrue(any(p['projectId']==record['contract']['projectId'] for p in TaskProjects(self.config).list()))

    def test_git_subdirectory_and_file_have_automatic_scopes(self):
        target=self.repo/'calc.py'
        record=self.service.create({'targetPath':str(target),'instruction':'Read the value','kind':'read_code'})
        self.assertEqual('sample',record['contract']['projectId'])
        self.assertEqual('calc.py',record['contract']['targetFile'])
        self.assertEqual(self.commit,record['contract']['baseCommit'])
        folder=self.repo/'frontend';folder.mkdir()
        nested=self.service.create({'targetPath':str(folder),'instruction':'Add a page'})
        self.assertEqual(['frontend'],nested['contract']['allowedPaths'])

    def test_directory_scope_and_unknown_targets(self):
        folder=self.root/'plain';folder.mkdir()
        record=self.service.create({'targetPath':str(folder),'instruction':'Inspect this folder','kind':'read_code'})
        self.assertIsNone(record['contract']['baseCommit'])
        with self.assertRaises(TaskError):
            self.service.create({'targetPath':str(folder/'missing.py'),'instruction':'change'})
        with self.assertRaises(TaskError):
            self.service.create({'targetPath':str(folder),'instruction':'change','workspaceMode':'worktree'})
