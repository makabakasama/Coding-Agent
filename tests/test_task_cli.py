"""CLI-to-HTTP checks against a temporary workbench and injected model."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import unittest
from unittest.mock import patch

from repo_agent.task_cli import main
from tests import test_tasks as fixtures
from tests import test_project_task_store as project_fixtures


class TaskCLITest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git
    service_for = project_fixtures.ProjectRoundsTest.service_for

    def invoke(self, port, *args):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(['--port', str(port), *args])
        return code, json.loads(output.getvalue() or error.getvalue())

    def test_shared_create_rounds_prepare_start_and_result(self):
        service = self.service_for()
        contract = self.root / 'request.json'
        contract.write_text(json.dumps(fixtures.request_contract(kind='read_code', allowedPaths=[],
                                                                 allowTests=False, checkTargets=[])), encoding='utf-8')
        with fixtures.serving(service) as port:
            code, first = self.invoke(port, 'create', '--file', str(contract))
            self.assertEqual(0, code)
            task_id = first['taskId']
            self.assertEqual(first, service.get(task_id))
            code, info = self.invoke(port, 'organization', task_id)
            self.assertEqual(0, code)
            key = info['taskKey']
            self.assertEqual(1, len(self.invoke(port, 'requirements', 'sample')[1]['tasks']))
            self.assertEqual('prepared', self.invoke(port, 'prepare', task_id)[1]['status'])
            self.assertEqual(0, self.invoke(port, 'start', task_id)[0])
            for thread in service.execution.threads:
                thread.join(20)
                self.assertFalse(thread.is_alive())
            self.assertEqual('terminated', self.invoke(port, 'status', task_id)[1]['status'])
            self.assertEqual('passed', self.invoke(port, 'result', task_id)[1]['review']['status'])
            self.assertGreater(len(self.invoke(port, 'events', task_id)[1]['events']), 0)
            code, second = self.invoke(port, 'new-round', 'sample', key)
            self.assertEqual(0, code)
            self.assertNotEqual(task_id, second['taskId'])
            self.assertEqual(2, len(self.invoke(port, 'rounds', 'sample', key)[1]['rounds']))
            self.assertEqual('not_started', self.invoke(port, 'status', second['taskId'])[1]['status'])

    def test_backend_error_and_invalid_file_have_nonzero_exit(self):
        with fixtures.serving(self.service_for()) as port:
            code, error = self.invoke(port, 'show', 'a' * 32)
            self.assertEqual(1, code)
            self.assertEqual('TASK_NOT_FOUND', error['error']['code'])
            path = self.root / 'bad.json'
            path.write_text('{', encoding='utf-8')
            self.assertEqual(2, self.invoke(port, 'create', '--file', str(path))[0])

    def test_lost_start_response_is_unknown_and_not_retried(self):
        with patch('repo_agent.task_cli.HTTPConnection') as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = ConnectionResetError('lost reply')
            code, error = self.invoke(4176, 'start', 'a' * 32)
        self.assertEqual(1, code)
        self.assertEqual('COMMAND_OUTCOME_UNKNOWN', error['error']['code'])
        connection.request.assert_called_once()
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
