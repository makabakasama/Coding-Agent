"""Project authorization and approvals against real HTTP, PostgreSQL and Redis."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from repo_agent.backend.repositories.project_access_store import ProjectAccessStore
from repo_agent.backend.services.project_access import ProjectAccess
from repo_agent.backend.services.task_execution import RecordedAgent
from repo_agent.cli.task_cli import main as task_cli
from repo_agent.core.decisions import Decision
from repo_agent.infrastructure.redis_cache import RedisCache
from repo_agent.infrastructure.redaction import Redactor
from tests import test_tasks as fixtures, test_workbench_auth as auth_fixtures
from tests.test_task_execution import ScriptedModel


class TraceRedactionTest(unittest.TestCase):
    def test_numeric_trace_measurements_survive_secret_redaction(self):
        with tempfile.TemporaryDirectory() as root:
            redactor = Redactor()
            redactor.secrets = {'12345678'}
            agent = RecordedAgent(None, None, trace_dir=Path(root), recorder=lambda event: None, redactor=redactor)
            agent._record({'type': 'run_start', 'input_bytes': 12345678, 'text': 'token 12345678'})
            record = json.loads(agent.trace_path.read_text(encoding='utf-8'))
        self.assertEqual(12345678, record['input_bytes'])
        self.assertEqual('token [REDACTED]', record['text'])


class ForbiddenProvider:
    def __init__(self, *args, **kwargs):
        raise AssertionError('This acceptance fixture cannot use a configured model provider')


class ProjectAccessFixture:
    git = fixtures.TaskIntegrationTest.git
    clear_test_sessions = auth_fixtures.WorkbenchAuthTest.clear_test_sessions
    login = auth_fixtures.WorkbenchAuthTest.login
    login_request = auth_fixtures.WorkbenchAuthTest.login_request
    request = auth_fixtures.WorkbenchAuthTest.request

    def setUp(self):
        self.enterContext(patch('repo_agent.backend.services.task_execution.OpenAICompatibleProvider', ForbiddenProvider))
        self.enterContext(patch('repo_agent.backend.services.chat_execution.OpenAICompatibleProvider', ForbiddenProvider))
        auth_fixtures.WorkbenchAuthTest.setUp(self)
        self.store = self.service.store
        self.store.cache = RedisCache(namespace=self.auth.namespace + ':reads', client=self.auth.client)
        executor = self.service.execution.execution
        executor.source = 'injected_test_model'
        executor.model_factory = ScriptedModel
        password = self.auth.hasher.hash(auth_fixtures.TEST_PASSWORD)
        for name in ('lead', 'other-lead', 'other-admin'):
            self.accounts.create(name, password, admin=name == 'other-admin')
        config = json.loads(self.config.read_text(encoding='utf-8'))
        for project in ('second', 'hidden'):
            subprocess.run(['git', 'clone', '--quiet', str(self.repo), str(self.root / project)],
                           check=True, capture_output=True, timeout=15)
            config['projects'].append({'projectId': project, 'name': project, 'repo': project})
        config['projects'].append({'projectId': 'alias', 'name': 'Alias', 'repo': 'repo'})
        self.config.write_text(json.dumps(config), encoding='utf-8')
        access = ProjectAccess(self.service, ProjectAccessStore(self.store))
        self.port = self.enterContext(fixtures.serving(self.service, auth=self.auth, access=access))
        self.admin = self.login()
        self.member = self.login('member')
        for project in ('sample', 'second', 'hidden'):
            self.expect(201, 'POST', f'/api/projects/{project}/enable', {'maintainer': 'lead'}, self.admin)
        self.member_role('sample', 'member', 'developer')
        self.member_role('second', 'member', 'observer')

    def expect(self, status, method, path, data=None, headers=None, code=None):
        actual, _, value = self.request(method, path, data, headers)
        self.assertEqual(status, actual, (method, path, value))
        if code:
            self.assertEqual(code, value['error']['code'])
        return value

    def member_role(self, project, login, role, *, actor=None, status=200, code=None):
        return self.expect(status, 'POST', f'/api/projects/{project}/members', {'login': login, 'role': role},
                           self.admin if actor is None else actor, code)

    def task(self, project='sample', *, mode='direct', actor=None):
        record = self.expect(201, 'POST', '/api/tasks',
                             fixtures.request_contract(projectId=project, workspaceMode=mode,
                                                       allowTests=False, checkTargets=[]),
                             self.admin if actor is None else actor)
        return record['taskId']

    def chat(self, project, actor=None):
        return self.expect(201, 'POST', '/api/conversations', {'workspaceRef': project},
                           self.admin if actor is None else actor)

    @staticmethod
    def turn_request(key, *, mode='discuss', revision=0, **execution):
        value = {'schemaVersion': 1, 'clientRequestId': key, 'expectedRevision': revision,
                 'mode': mode, 'content': 'Local authorization fixture'}
        if mode == 'execute':
            value['execution'] = {'allowedPaths': ['calc.py'], 'allowTests': False,
                                  'workspaceMode': 'direct', **execution}
        return value

    def counts(self):
        with self.store._connection() as conn:
            return conn.execute("""SELECT (SELECT count(*) FROM task_dispatch),
                (SELECT count(*) FROM chat_turn_jobs)""").fetchone()

    def task_application(self, task_id):
        return self.expect(202, 'POST', f'/api/tasks/{task_id}/execution', {}, self.member)

    def decide(self, application, decision='approve', *, actor=None, status=200, code=None):
        return self.expect(status, 'POST', application['statusUrl'], {'decision': decision},
                           self.admin if actor is None else actor, code)

    @staticmethod
    def concurrently(first, second):
        gate = threading.Barrier(2)

        def run(operation):
            gate.wait(timeout=4)
            return operation()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, operation) for operation in (first, second)]
            return [future.result(timeout=12) for future in futures]


@unittest.skipUnless(auth_fixtures.DATABASE and auth_fixtures.REDIS, 'Dedicated PostgreSQL and Redis required')
class ProjectAccessTest(ProjectAccessFixture, unittest.TestCase):
    def test_roles_task_lists_chat_pages_and_private_conversations(self):
        sample = self.task(actor=self.member)
        readonly = self.task('second')
        hidden = self.task('hidden')
        projects = self.expect(200, 'GET', '/api/projects', headers=self.member)['projects']
        self.assertEqual({'sample': 'developer', 'second': 'observer'},
                         {item['projectId']: item['role'] for item in projects})
        records = self.expect(200, 'GET', '/api/tasks', headers=self.member)['tasks']
        self.assertEqual({sample, readonly}, {item['taskId'] for item in records})
        self.expect(401, 'GET', f'/api/tasks/{sample}')
        for suffix in ('', '/execution/events', '/execution/artifacts/diff'):
            self.expect(404, 'GET', f'/api/tasks/{hidden}{suffix}', headers=self.member)
        self.expect(404, 'GET', '/api/projects/hidden/tasks', headers=self.member)
        self.expect(403, 'POST', f'/api/tasks/{readonly}/execution', {}, self.member, 'ROLE_REQUIRED')
        self.expect(403, 'GET', f'/api/tasks/{sample}/execution/diagnostics', headers=self.member)
        self.expect(403, 'GET', '/api/local-files', headers=self.member)
        self.expect(403, 'POST', '/api/tasks', {'instruction': 'Fixture', 'targetPath': str(self.repo)}, self.member)
        self.expect(409, 'POST', '/api/projects/alias/enable', {'maintainer': 'lead'},
                    self.admin, 'PROJECT_SOURCE_OVERLAP')

        self.chat('hidden')
        private = self.chat(None, self.member)['conversationId']
        legacy = self.service.chat.create({'workspaceRef': None})['conversationId']
        visible = {private, self.chat('sample', self.member)['conversationId'],
                   self.chat('second')['conversationId']}
        found, cursor = [], None
        for _ in range(5):
            query = '?limit=1' + ('&cursor=' + cursor if cursor else '')
            page = self.expect(200, 'GET', '/api/conversations' + query, headers=self.member)
            found.extend(chat['conversationId'] for chat in page['conversations'])
            cursor = page['nextCursor']
            if not cursor:
                break
        self.assertEqual(visible, set(found))
        self.assertEqual(len(visible), len(found))
        self.expect(404, 'GET', f'/api/conversations/{private}', headers=self.admin)
        self.expect(404, 'GET', f'/api/conversations/{legacy}', headers=self.member)
        self.expect(200, 'GET', f'/api/conversations/{legacy}', headers=self.admin)

    def test_warm_cache_and_open_sse_enforce_member_revocation(self):
        task_id = self.task(actor=self.member)
        path = f'/api/tasks/{task_id}'
        self.expect(200, 'GET', path, headers=self.member)
        cache_key = self.store.cache.key('run:' + task_id)
        self.assertIsNotNone(self.auth.client.get(cache_key))
        conversation = self.chat('sample', self.member)['conversationId']
        turns = f'/api/conversations/{conversation}/turns'
        turn = self.expect(202, 'POST', turns, self.turn_request('stream-1'), self.member)
        stream = turns + f'/{turn["turnId"]}/events?stream=1'
        with closing(HTTPConnection('127.0.0.1', self.port, timeout=5)) as client:
            client.request('GET', stream, headers=self.member)
            response = client.getresponse()
            self.assertEqual(200, response.status)
            self.assertEqual(b'retry: 500\n', response.readline())
            self.member_role('sample', 'member', None)
            start = time.monotonic()
            response.read()
            self.assertLess(time.monotonic() - start, 4)
        self.assertIsNotNone(self.auth.client.get(cache_key))
        self.expect(200, 'GET', '/api/auth/me', headers=self.member)
        self.expect(404, 'GET', path, headers=self.member)
        self.expect(404, 'GET', stream, headers=self.member)

    def test_task_concurrent_approval_enqueues_once_and_defers_preparation(self):
        task_id = self.task(mode='worktree', actor=self.member)
        application = self.task_application(task_id)
        self.assertEqual('pending', application['executionRequest']['status'])
        self.assertEqual((0, 0), self.counts())
        self.assertEqual('not_prepared', self.expect(200, 'GET', f'/api/tasks/{task_id}/preparation',
                                                     headers=self.member)['status'])
        self.decide(application, actor=self.login('lead'), status=403, code='ADMIN_REQUIRED')
        other_admin = self.login('other-admin')
        first, second = self.concurrently(lambda: self.decide(application),
                                          lambda: self.decide(application, actor=other_admin))
        self.assertEqual(first, second)
        self.assertEqual(task_id, first['execution']['taskId'])
        self.assertEqual((1, 0), self.counts())
        self.assertEqual('not_prepared', self.expect(200, 'GET', f'/api/tasks/{task_id}/preparation',
                                                     headers=self.member)['status'])
        self.assertEqual(first['execution']['executionId'], self.service.execution.queue.get(task_id, fresh=True)['executionId'])

    def test_rejected_and_revoked_requesters_never_enqueue(self):
        denied = self.task_application(self.task(actor=self.member))
        self.assertEqual('rejected', self.decide(denied, 'reject')['status'])
        self.decide(denied, status=409, code='REQUEST_ALREADY_DECIDED')
        revoked = self.task_application(self.task(actor=self.member))
        self.member_role('sample', 'member', 'observer')
        self.decide(revoked, status=409, code='REQUESTER_PERMISSION_CHANGED')
        self.member_role('sample', 'member', 'developer')
        disabled = self.task_application(self.task(actor=self.member))
        self.accounts.set_enabled('member', False)
        self.decide(disabled, status=409, code='REQUESTER_DISABLED')
        self.assertEqual((0, 0), self.counts())

    def test_last_maintainer_survives_concurrent_role_changes(self):
        lead = self.login('lead')
        self.member_role('sample', 'other-lead', 'maintainer', actor=lead)
        path = '/api/projects/sample/members'
        results = self.concurrently(
            lambda: self.request('POST', path, {'login': 'lead', 'role': 'observer'}, self.admin),
            lambda: self.request('POST', path, {'login': 'other-lead', 'role': None}, self.admin),
        )
        self.assertEqual([200, 409], sorted(item[0] for item in results))
        self.assertEqual('LAST_MAINTAINER', next(item[2]['error']['code'] for item in results if item[0] == 409))
        members = self.expect(200, 'GET', path, headers=self.admin)['members']
        self.assertEqual(1, len([item for item in members if item['role'] == 'maintainer' and item['enabled']]))

    def test_chat_approval_freezes_commit_and_enqueues_once(self):
        conversation = self.chat('sample', self.member)['conversationId']
        turns = f'/api/conversations/{conversation}/turns'
        request = self.turn_request('chat-code-1', mode='execute')
        application = self.expect(202, 'POST', turns, request, self.member)
        self.assertEqual(self.commit, application['executionRequest']['snapshot']['frozenBaseCommit'])
        self.assertEqual((0, 0), self.counts())
        self.git('-c', 'user.name=Offline', '-c', 'user.email=offline@example.invalid',
                 'commit', '--allow-empty', '-qm', 'move fixture HEAD')
        self.assertEqual(application, self.expect(202, 'POST', turns, request, self.member))
        other_admin = self.login('other-admin')
        first, second = self.concurrently(lambda: self.decide(application),
                                          lambda: self.decide(application, actor=other_admin))
        self.assertEqual(first, second)
        self.assertEqual((0, 1), self.counts())
        turn = self.expect(200, 'GET', first['execution']['statusUrl'], headers=self.member)
        self.assertEqual(self.commit, turn['policySnapshot']['frozenBaseCommit'])
        self.assertEqual(first['execution']['turnId'], first['execution']['taskId'])

    def test_rework_pins_previous_patch_and_project_role_controls_delivery(self):
        task_id = self.task(actor=self.member)
        approval = self.decide(self.task_application(task_id))
        executor = self.service.execution
        executor.execution.model_factory = lambda: ScriptedModel([
            Decision('replace_text', {'path': 'calc.py', 'old_text': 'value = 1', 'new_text': 'value = 2'}),
            Decision('finish', {'answer': 'Fixture change', 'outcome': 'completed'}),
        ])
        claim = executor.queue.claim('fixture-worker')
        self.assertEqual((task_id, approval['execution']['executionId']), claim)
        state = executor.execution.run_claimed(*claim)
        self.assertEqual('terminated', state['status'])
        executor.queue.finish(task_id, 'fixture-worker', terminated=True, reason=state['stopReason'])
        diff = self.expect(200, 'GET', f'/api/tasks/{task_id}/execution/artifacts/diff', headers=self.member)
        self.assertIn('+value = 2', diff['content'], executor.diagnostics(task_id))
        self.expect(404, 'GET', f'/api/tasks/{task_id}/execution/artifacts/diff', headers=self.login('other-lead'))
        decision = {'executionId': claim[1], 'decision': 'accepted'}
        self.expect(403, 'POST', f'/api/tasks/{task_id}/review', decision, self.member)
        self.expect(200, 'POST', f'/api/tasks/{task_id}/review', decision, self.login('lead'))

        conversation = self.chat('sample', self.member)['conversationId']
        turns = f'/api/conversations/{conversation}/turns'
        rework = self.turn_request('rework-1', mode='execute', parentTaskId=task_id)
        application = self.expect(202, 'POST', turns, rework, self.member)
        snapshot = application['executionRequest']['snapshot']
        self.assertEqual(task_id, snapshot['reviewBase']['parentTaskId'])
        self.assertTrue(snapshot['reviewBase']['patchSha256'])
        self.assertEqual((1, 0), self.counts())
        approved = self.decide(application)
        self.assertEqual((1, 1), self.counts())
        turn = self.expect(200, 'GET', approved['execution']['statusUrl'], headers=self.member)
        self.assertEqual(snapshot['reviewBase'], turn['policySnapshot']['reviewBase'])

    def test_cli_approval_uses_same_api_and_account_namespace_cannot_use_legacy_entry(self):
        task_id = self.task(actor=self.member)
        saved = {}

        def invoke(*args):
            with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
                code = task_cli(['--port', str(self.port), *args])
            return code, json.loads(output.getvalue() or errors.getvalue())

        def remember(service, key, value):
            saved[service, key] = value

        with (patch('keyring.get_password', side_effect=lambda *key: saved.get(key)),
              patch('keyring.set_password', side_effect=remember),
              patch('keyring.delete_password', side_effect=lambda *key: saved.pop(key)),
              patch('repo_agent.cli.account_admin.prompt_password', return_value=auth_fixtures.TEST_PASSWORD)):
            self.assertEqual(0, invoke('login', 'member')[0])
            code, application = invoke('start', task_id)
            self.assertEqual(0, code)
            request_id = application['executionRequest']['requestId']
            self.assertEqual(1, invoke('approve', 'sample', request_id)[0])
            self.assertEqual((0, 0), self.counts())
            self.assertEqual(0, invoke('logout')[0])
            self.assertEqual(0, invoke('login', 'operator')[0])
            self.assertEqual(0, invoke('approve', 'sample', request_id)[0])
            self.assertEqual((1, 0), self.counts())
        other = self.task('second')
        with fixtures.serving(self.service) as port:
            status, _, execution = fixtures.http(port, 'POST', f'/api/tasks/{other}/execution', {})
        self.assertEqual(503, status)
        self.assertEqual('ACCOUNT_MODE_REQUIRED', execution['error']['code'])
        self.assertEqual((1, 0), self.counts())
