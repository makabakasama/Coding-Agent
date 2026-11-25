"""Terminal logging must describe progress without leaking task content."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from repo_agent.backend.services.worker_pool import WorkerPool
from repo_agent.backend.schemas.task_contract import TaskError


class WorkerLoggingTest(unittest.TestCase):
    def setUp(self):
        self.queue = Mock()
        self.queue.needs_guarded_preparation.return_value = False

    def test_completion_reports_identity_and_reason(self):
        execution = SimpleNamespace(run_claimed=Mock(return_value={
            'status': 'terminated', 'stopReason': 'agent_reported_finished'}))
        pool = WorkerPool(self.queue, execution)
        with self.assertLogs('repo_agent.worker', level='INFO') as captured:
            pool._execute('a' * 32, 'b' * 32)
        text = '\n'.join(captured.output)
        self.assertIn('task=' + 'a' * 32, text)
        self.assertIn('execution=' + 'b' * 32, text)
        self.assertIn('started', text)
        self.assertIn('finished status=terminated reason=agent_reported_finished', text)
        self.queue.finish.assert_called_once()

    def test_unexpected_exception_logs_type_without_private_message(self):
        execution = SimpleNamespace(run_claimed=Mock(side_effect=RuntimeError('private-token /private/source')))
        with self.assertLogs('repo_agent.worker', level='INFO') as captured:
            WorkerPool(self.queue, execution)._execute('a' * 32, 'b' * 32)
        text = '\n'.join(captured.output)
        self.assertIn('error type=RuntimeError', text)
        self.assertIn('finished status=unknown reason=WORKER_EXECUTION_ERROR', text)
        self.assertNotIn('private-token', text)
        self.assertNotIn('/private/source', text)

    def test_preflight_failure_is_reported_as_blocked(self):
        execution = SimpleNamespace(run_claimed=Mock(side_effect=TaskError(
            409, 'WORKSPACE_DIRTY', 'private details')))
        with self.assertLogs('repo_agent.worker', level='INFO') as captured:
            WorkerPool(self.queue, execution)._execute('a' * 32, 'b' * 32)
        self.assertIn('blocked reason=WORKSPACE_DIRTY', '\n'.join(captured.output))
        self.assertNotIn('private details', '\n'.join(captured.output))
