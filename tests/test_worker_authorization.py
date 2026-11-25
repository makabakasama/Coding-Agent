"""Worker authorization against PostgreSQL, with model HTTP disabled."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.worker_pool import WorkerPool
from repo_agent.core.decisions import Decision
from tests import test_workbench_auth as auth_fixtures
from tests.chat_fixture import FixtureTokenBudget
from tests.test_project_access import ProjectAccessFixture
from tests.test_task_execution import ScriptedModel


GUARDED_WORKER = """
from pathlib import Path
import sys
import threading
from unittest.mock import patch

from repo_agent.backend import worker
from repo_agent.backend.services.worker_pool import WorkerPool

stop, forbidden = map(Path, sys.argv[1:3])
sys.argv = ['worker', *sys.argv[3:]]
real_run = WorkerPool.run

def denied(*args, **kwargs):
    forbidden.touch()
    raise AssertionError('Provider is forbidden in the worker fixture')

def controlled_run(pool, stop_event):
    def monitor():
        while not stop_event.wait(0.05):
            if stop.is_file():
                stop_event.set()
    threading.Thread(target=monitor, daemon=True).start()
    return real_run(pool, stop_event)

with (patch.object(WorkerPool, 'run', controlled_run),
      patch('repo_agent.backend.services.task_execution.OpenAICompatibleProvider', denied),
      patch('repo_agent.backend.services.chat_execution.OpenAICompatibleProvider', denied),
      patch('repo_agent.core.provider.urllib.request.urlopen', denied)):
    worker.main()
"""


@unittest.skipUnless(auth_fixtures.DATABASE and auth_fixtures.REDIS, 'Dedicated PostgreSQL and Redis required')
class WorkerAuthorizationTest(ProjectAccessFixture, unittest.TestCase):
    def setUp(self):
        outbound = self.enterContext(patch('repo_agent.core.provider.urllib.request.urlopen',
                                          side_effect=AssertionError('Model HTTP is forbidden')))
        self.addCleanup(outbound.assert_not_called)
        super().setUp()
        executor = self.service.execution.execution
        self.chat_worker = self.service.chat_execution
        self.chat_worker.token_counter_factory = FixtureTokenBudget
        executor.token_counter_factory = FixtureTokenBudget
        self.queue = self.service.execution.queue
        self.jobs = self.service.chat.store
        self.worker = WorkerPool(self.queue, executor, chat=self.chat_worker)
        self.source_before = (self.repo / 'calc.py').read_bytes()
        self.decisions = []

        class LocalModel(ScriptedModel):
            def decide(model, messages):
                self.decisions.append(True)
                return super().decide(messages)

        executor.model_factory = lambda: LocalModel([
            Decision('replace_text', {'path': 'calc.py', 'old_text': 'value = 1', 'new_text': 'value = 2'}),
            Decision('finish', {'answer': 'Fixture change', 'outcome': 'completed'}),
        ])

    def approve_task(self, *, mode='direct'):
        task = self.task(mode=mode, actor=self.member)
        return self.decide(self.task_application(task))['execution']

    def approve_chat(self, name):
        conversation = self.chat('sample', self.member)['conversationId']
        application = self.expect(202, 'POST', f'/api/conversations/{conversation}/turns',
                                  self.turn_request(name, mode='execute'), self.member)
        return self.decide(application)['execution']

    def audit(self, execution, headers=None):
        return self.expect(200, 'GET', f'/api/projects/sample/executions/{execution}/audit',
                           headers=self.admin if headers is None else headers)['events']

    def assert_task_denied(self, binding, reason):
        self.assertIsNone(self.queue.claim(self.worker.worker_id))
        state = self.expect(200, 'GET', binding['statusUrl'], headers=self.admin)
        self.assertEqual(('terminated', reason, False, True),
                         (state['status'], state['stopReason'], state['resultAvailable'], state['authorizationDenied']))
        events = self.audit(binding['executionId'])
        self.assertIn(('task_authorization_denied', reason), [(item['type'], item['reason']) for item in events])
        self.assertNotIn('task_claimed', [item['type'] for item in events])

    def assert_no_execution(self):
        self.assertEqual([], self.decisions)
        self.assertEqual(self.source_before, (self.repo / 'calc.py').read_bytes())

    def test_downgrade_after_approval_blocks_task_without_preparation(self):
        binding = self.approve_task(mode='worktree')
        self.member_role('sample', 'member', 'observer')
        self.assert_task_denied(binding, 'AUTH_REQUESTER_PERMISSION_REVOKED')
        prep = self.expect(200, 'GET', f'/api/tasks/{binding["taskId"]}/preparation', headers=self.admin)
        self.assertEqual('not_prepared', prep['status'])
        self.assert_no_execution()

    def run_independent_worker_to_termination(self, binding):
        stop = self.root / 'stop-worker'
        forbidden = self.root / 'provider-access'
        log = self.root / 'worker.log'
        env = {**os.environ, 'DATABASE_URL': self.store.pool.conninfo, 'REDIS_URL': auth_fixtures.REDIS,
               'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        namespace = sha256((env['DATABASE_URL'] + str(self.store.root) + str(self.store.runs_root)).encode()).hexdigest()

        def clear_worker_cache():
            for key in self.auth.client.scan_iter(match='coding-agent:v1:' + namespace + ':*'):
                self.auth.client.delete(key)

        self.addCleanup(clear_worker_cache)
        args = [sys.executable, '-B', '-u', '-c', GUARDED_WORKER, str(stop), str(forbidden),
                '--auth', '--workers', '1', '--data-dir', str(self.store.root),
                '--runs-root', str(self.store.runs_root), '--project-workspaces', str(self.store.project_workspaces),
                '--projects', str(self.config)]
        with log.open('wb') as output:
            child = subprocess.Popen(args, env=env, stdout=output, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and child.poll() is None:
                    if self.queue.get(binding['taskId'], fresh=True)['status'] == 'terminated':
                        break
                    time.sleep(.05)
                else:
                    self.fail(log.read_text(encoding='utf-8'))
            finally:
                stop.touch()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        self.assertEqual(0, child.returncode, log.read_text(encoding='utf-8'))
        self.assertFalse(forbidden.exists())

    def test_independent_worker_rejects_disabled_requester_without_model_or_worktree(self):
        binding = self.approve_task(mode='worktree')
        self.accounts.set_enabled('member', False)
        self.run_independent_worker_to_termination(binding)
        self.assert_task_denied(binding, 'AUTH_REQUESTER_DISABLED')
        prep = self.expect(200, 'GET', f'/api/tasks/{binding["taskId"]}/preparation', headers=self.admin)
        self.assertEqual('not_prepared', prep['status'])
        self.assert_no_execution()

    def test_disabled_requester_and_approver_are_distinct(self):
        requester = self.approve_task()
        self.accounts.set_enabled('member', False)
        self.assert_task_denied(requester, 'AUTH_REQUESTER_DISABLED')
        self.accounts.set_enabled('member', True)
        self.member = self.login('member')
        approver = self.approve_task()
        self.accounts.set_enabled('operator', False)
        self.admin = self.login('other-admin')
        self.assert_task_denied(approver, 'AUTH_APPROVER_REVOKED')
        self.assert_no_execution()

    def test_frozen_contract_drift_and_old_unbound_dispatch_are_blocked(self):
        binding = self.approve_task()
        with self.store._connection() as conn:
            conn.execute("""UPDATE task_runs SET record=jsonb_set(record,'{contract,allowedPaths}','[]')
                WHERE task_id=%s""", (binding['taskId'],))
        self.assert_task_denied(binding, 'AUTH_SCOPE_MISMATCH')
        old = self.approve_task()
        with self.store._connection() as conn:
            conn.execute('UPDATE task_dispatch SET authorization_proof=NULL WHERE task_id=%s', (old['taskId'],))
        self.assert_task_denied(old, 'AUTH_APPROVAL_REQUIRED')
        self.assert_no_execution()

    def test_chat_bridge_and_coding_claim_each_enforce_revocation(self):
        early = self.approve_chat('deny-bridge')
        self.member_role('sample', 'member', None)
        self.assertIsNone(self.jobs.claim(self.worker.worker_id))
        stopped = self.expect(200, 'GET', early['statusUrl'], headers=self.admin)
        self.assertEqual(('failed', 'AUTH_REQUESTER_PERMISSION_REVOKED'),
                         (stopped['status'], stopped['stopReason']))
        self.assertEqual((0, 1), self.counts())
        self.assertFalse((self.root / 'workspaces' / 'sample').exists())

        self.member_role('sample', 'member', 'developer')
        late = self.approve_chat('deny-coding')
        claim = self.jobs.claim(self.worker.worker_id)
        self.assertEqual(late['turnId'], claim['turnId'])
        self.chat_worker.run_claimed(claim)
        queued = self.queue.get(late['taskId'], fresh=True)
        if queued is None:
            turn = self.expect(200, 'GET', late['statusUrl'], headers=self.admin)
            self.fail(f'Bridge did not enqueue: {turn["stopReason"]}, {turn.get("error")}')
        self.assertEqual(late['executionId'], queued['executionId'])
        self.accounts.set_enabled('member', False)
        self.assertIsNone(self.queue.claim(self.worker.worker_id))
        self.chat_worker.reconcile()
        stopped = self.expect(200, 'GET', late['statusUrl'], headers=self.admin)
        self.assertEqual(('failed', 'AUTH_REQUESTER_DISABLED'), (stopped['status'], stopped['stopReason']))
        types = [event['type'] for event in self.audit(late['executionId'])]
        self.assertIn('chat_claimed', types)
        self.assertIn('task_authorization_denied', types)
        self.assertNotIn('task_claimed', types)
        self.assert_no_execution()

    def test_discussion_checks_actor_without_coding_approval(self):
        conversation = self.chat('sample', self.member)['conversationId']
        turns = f'/api/conversations/{conversation}/turns'
        valid = self.expect(202, 'POST', turns, self.turn_request('discussion-ok'), self.member)
        claim = self.jobs.claim(self.worker.worker_id)
        self.assertEqual(valid['turnId'], claim['turnId'])
        self.assertNotIn('authorization', claim)
        self.jobs.finish(claim, {'status': 'failed', 'stopReason': 'FIXTURE_NO_MODEL'})
        revision = self.expect(200, 'GET', f'/api/conversations/{conversation}', headers=self.member)['revision']
        denied = self.expect(202, 'POST', turns, self.turn_request('discussion-removed', revision=revision), self.member)
        self.member_role('sample', 'member', None)
        self.assertIsNone(self.jobs.claim(self.worker.worker_id))
        stopped = self.expect(200, 'GET', f'{turns}/{denied["turnId"]}', headers=self.admin)
        self.assertEqual('AUTH_REQUESTER_PERMISSION_REVOKED', stopped['stopReason'])
        self.assert_no_execution()

    def test_revoke_committed_before_waiting_claim_is_authoritative(self):
        binding = self.approve_task()
        claimant_started = threading.Event()

        def claim():
            claimant_started.set()
            return self.queue.claim(self.worker.worker_id)

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.store._connection() as conn:
                self.service.audit.lock(conn)
                future = executor.submit(claim)
                self.assertTrue(claimant_started.wait(4))
                conn.execute("""DELETE FROM workbench_project_members
                    WHERE namespace=%s AND project_id='sample' AND account_id=
                        (SELECT account_id FROM workbench_accounts WHERE login='member')""",
                             (self.service.audit.namespace,))
            self.assertIsNone(future.result(timeout=8))
        self.assertEqual('AUTH_REQUESTER_PERMISSION_REVOKED',
                         self.queue.get(binding['taskId'], fresh=True)['reason'])
        self.assert_no_execution()

    def test_successful_claim_precedes_waiting_revocation(self):
        binding = self.approve_task()
        verified, release = threading.Event(), threading.Event()
        original = self.service.audit.authorize_task

        def authorize(*args):
            result = original(*args)
            verified.set()
            if not release.wait(4):
                raise AssertionError('Fixture did not release the claim')
            return result

        with patch.object(self.service.audit, 'authorize_task', side_effect=authorize):
            with ThreadPoolExecutor(max_workers=2) as executor:
                claim = executor.submit(self.queue.claim, self.worker.worker_id)
                try:
                    self.assertTrue(verified.wait(4))
                    revoke = executor.submit(self.member_role, 'sample', 'member', None)
                finally:
                    release.set()
                self.assertEqual((binding['taskId'], binding['executionId']), claim.result(timeout=8))
                revoke.result(timeout=8)
        self.assertIsNone(self.queue.claim(self.worker.worker_id))
        self.assert_no_execution()

    def test_claim_transaction_failure_preserves_queue_and_can_retry(self):
        binding = self.approve_task()
        audit = self.service.audit

        def fail_in_transaction(conn, *args, **kwargs):
            conn.execute('SELECT 1 / 0')

        with patch.object(audit, 'job_event', side_effect=fail_in_transaction):
            with self.assertRaises(TaskError) as error:
                self.queue.claim(self.worker.worker_id)
        self.assertEqual((503, 'DATABASE_UNAVAILABLE'), (error.exception.status, error.exception.code))
        self.assertEqual('queued', self.queue.get(binding['taskId'], fresh=True)['status'])
        types = {event['type'] for event in self.audit(binding['executionId'])}
        self.assertFalse(types & {'task_claimed', 'task_authorization_denied', 'task_authorization_passed'})
        self.assertEqual((binding['taskId'], binding['executionId']), self.queue.claim(self.worker.worker_id))
        self.assert_no_execution()

    def test_recovery_reconciles_saved_result_and_audit_without_reexecution(self):
        binding = self.approve_task()
        task, execution = self.queue.claim(self.worker.worker_id)
        self.assertEqual((binding['taskId'], binding['executionId']), (task, execution))
        with patch.object(self.queue, 'finish', side_effect=TaskError(503, 'DATABASE_UNAVAILABLE', 'Fixture')):
            with self.assertRaises(TaskError):
                self.worker._execute(task, execution)
        self.assertEqual('running', self.queue.get(task, fresh=True)['status'])
        decisions = len(self.decisions)
        self.assertGreater(decisions, 0)
        self.run_independent_worker_to_termination(binding)
        self.queue.abandon_claims(recover=self.worker._saved_termination)
        self.assertEqual('terminated', self.queue.get(task, fresh=True)['status'])
        self.assertIsNone(self.queue.claim(self.worker.worker_id))
        self.assertEqual(decisions, len(self.decisions))
        diff = self.expect(200, 'GET', f'/api/tasks/{task}/execution/artifacts/diff', headers=self.member)
        self.assertIn('+value = 2', diff['content'])
        maintainer = self.login('lead')
        review = {'executionId': execution, 'decision': 'accepted'}
        for _ in range(2):
            self.expect(200, 'POST', f'/api/tasks/{task}/review', review, maintainer)
        events = self.audit(execution, self.member)
        for kind, login in (('request_submitted', 'member'), ('request_approved', 'operator'),
                            ('review_accepted', 'lead')):
            matching = [event for event in events if event['type'] == kind]
            self.assertEqual([login], [event['login'] for event in matching])
        finished = [event for event in events if event['type'] == 'task_finished']
        self.assertEqual([self.worker.worker_id], [event['workerId'] for event in finished])
        self.assertTrue(next(event for event in events if event['type'] == 'review_accepted')['artifactSha256'])
        self.expect(404, 'GET', f'/api/projects/sample/executions/{execution}/audit',
                    headers=self.login('other-lead'))

    def test_chat_rework_keeps_one_execution_id_through_delivery_and_review(self):
        parent = self.approve_task()
        self.worker._execute(*self.queue.claim(self.worker.worker_id))
        self.assertIn(b'value = 2', (self.repo / 'calc.py').read_bytes())
        admin_review = {'executionId': parent['executionId'], 'decision': 'changes_requested'}
        self.expect(200, 'POST', f'/api/tasks/{parent["taskId"]}/review', admin_review, self.login('lead'))
        cid = self.chat('sample', self.member)['conversationId']
        request = self.turn_request('approved-rework', mode='execute', parentTaskId=parent['taskId'])
        application = self.expect(202, 'POST', f'/api/conversations/{cid}/turns', request, self.member)
        binding = self.decide(application)['execution']
        self.worker.execution.model_factory = lambda: ScriptedModel([
            Decision('replace_text', {'path': 'calc.py', 'old_text': 'value = 2', 'new_text': 'value = 3'}),
            Decision('finish', {'answer': 'Fixture follow-up', 'outcome': 'completed'}),
        ])
        claimed = self.jobs.claim(self.worker.worker_id)
        self.assertEqual(binding['turnId'], claimed['turnId'])
        self.chat_worker.run_claimed(claimed)
        task_claim = self.queue.claim(self.worker.worker_id)
        self.assertEqual((binding['taskId'], binding['executionId']), task_claim)
        self.worker._execute(*task_claim)
        self.chat_worker.reconcile()
        self.assertIn(b'value = 3', (self.repo / 'calc.py').read_bytes())
        turn = self.expect(200, 'GET', binding['statusUrl'], headers=self.member)
        self.assertEqual(('completed', binding['executionId']),
                         (turn['status'], turn['executionEvidence']['executionId']))
        review = {'executionId': binding['executionId'], 'decision': 'accepted'}
        self.expect(200, 'POST', f'/api/tasks/{binding["taskId"]}/review', review, self.login('lead'))
        events = self.audit(binding['executionId'])
        types = [event['type'] for event in events]
        for kind in ('chat_claimed', 'task_claimed', 'task_finished', 'chat_finished', 'review_accepted'):
            self.assertEqual(1, types.count(kind), types)
        self.assertEqual({binding['executionId']}, {event['executionId'] for event in events if event['executionId']})
