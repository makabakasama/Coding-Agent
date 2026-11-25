"""Account acceptance against dedicated PostgreSQL and Redis test services."""
from contextlib import redirect_stderr, redirect_stdout
from http.cookies import SimpleCookie
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch
import uuid

from repo_agent.backend.repositories.account_store import AccountStore
from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.routers.auth_http import CSRF_HEADER, cookie_name
from repo_agent.backend.services.task_service import TaskService
from repo_agent.cli.task_cli import main as task_cli
from tests import test_tasks as fixtures


DATABASE = os.environ.get('CODING_AGENT_TEST_DATABASE_URL')
REDIS = os.environ.get('CODING_AGENT_TEST_REDIS_URL')
TEST_PASSWORD = 'example-password-for-local-fixtures'


@unittest.skipUnless(DATABASE and REDIS, 'Dedicated PostgreSQL and Redis test services required')
class WorkbenchAuthTest(unittest.TestCase):
    git = fixtures.TaskIntegrationTest.git

    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from redis.exceptions import ConnectionError
        from repo_agent.backend.services.auth_service import AuthService, FAILURE_LIMIT, SESSION_SECONDS

        fixtures.TaskIntegrationTest.setUp(self)
        self.failure_limit = FAILURE_LIMIT
        self.session_seconds = SESSION_SECONDS
        self.redis_error = ConnectionError('Injected session Redis outage')
        schema = 'workbench_auth_test_' + uuid.uuid4().hex

        def schema_command(statement):
            with psycopg.connect(DATABASE, connect_timeout=5) as conn:
                conn.execute(sql.SQL(statement).format(sql.Identifier(schema)))

        schema_command('CREATE SCHEMA {}')
        self.addCleanup(schema_command, 'DROP SCHEMA {} CASCADE')
        options = conninfo_to_dict(DATABASE).get('options', '')
        dsn = make_conninfo(DATABASE, options=f'{options} -csearch_path={schema}'.strip())
        store = DatabaseTaskStore(self.data, self.root / 'runs', self.root / 'workspaces', dsn)
        self.addCleanup(store.close)
        self.service = TaskService(store, TaskProjects(self.config), project_root=self.root,
                                   workspace_root=self.root / 'legacy')
        self.accounts = AccountStore(store._connection)
        self.auth = AuthService(self.accounts, REDIS, dsn)
        self.addCleanup(self.auth.close)
        self.addCleanup(self.clear_test_sessions)
        password_hash = self.auth.hasher.hash(TEST_PASSWORD)
        self.assertTrue(password_hash.startswith('$argon2id$'))
        self.accounts.create('operator', password_hash, admin=True)
        self.accounts.create('member', password_hash)
        self.port = self.enterContext(fixtures.serving(self.service, auth=self.auth))

    def clear_test_sessions(self):
        for key in self.auth.client.scan_iter(match=self.auth.namespace + ':*'):
            self.auth.client.delete(key)

    def request(self, method, path, data=None, headers=None):
        return fixtures.http(self.port, method, path, data, headers)

    def login_request(self, login, password=TEST_PASSWORD):
        return self.request('POST', '/api/auth/login', {'login': login, 'password': password})

    def login(self, login='operator'):
        status, headers, identity = self.login_request(login)
        self.assertEqual(200, status)
        name = cookie_name(self.port)
        session = SimpleCookie(headers['Set-Cookie'])[name]
        self.assertTrue(session['httponly'])
        self.assertEqual('Strict', session['samesite'])
        self.assertEqual(str(self.session_seconds), session['max-age'])
        return {'Cookie': f'{name}={session.value}', CSRF_HEADER: identity['csrfToken']}

    def test_logout_revokes_one_session_and_disable_revokes_all_old_versions(self):
        first, second = self.login(' Operator '), self.login()
        self.assertNotEqual(first['Cookie'], second['Cookie'])
        self.assertEqual(200, self.request('POST', '/api/auth/logout', headers=first)[0])
        self.assertEqual(401, self.request('GET', '/api/auth/me', headers=first)[0])
        self.assertEqual(200, self.request('GET', '/api/auth/me', headers=second)[0])

        third = self.login()
        self.accounts.set_enabled('operator', False)
        for session in (second, third):
            self.assertEqual(401, self.request('GET', '/api/auth/me', headers=session)[0])
        self.assertEqual(401, self.login_request('operator')[0])
        self.accounts.set_enabled('operator', True)
        for session in (second, third):
            self.assertEqual(401, self.request('GET', '/api/auth/me', headers=session)[0])
        self.login()

    def test_redis_failure_preserves_cookie_and_expiry_returns_401_after_recovery(self):
        session = self.login()
        with patch.object(self.auth.client, 'get', side_effect=self.redis_error):
            status, _, error = self.request('GET', '/api/auth/me', headers=session)
            self.assertEqual((503, 'SESSION_STORE_UNAVAILABLE'), (status, error['error']['code']))
        with patch.object(self.auth.client, 'delete', side_effect=self.redis_error):
            status, headers, _ = self.request('POST', '/api/auth/logout', headers=session)
            self.assertEqual(503, status)
            self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(200, self.request('GET', '/api/auth/me', headers=session)[0])

        token = session['Cookie'].split('=', 1)[1]
        key = self.auth._session_key(token)
        ttl = self.auth.client.ttl(key)
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, self.session_seconds)
        self.assertTrue(self.auth.client.expireat(key, 1))
        self.assertEqual(401, self.request('GET', '/api/auth/me', headers=session)[0])

    def test_restored_redis_record_cannot_revive_a_logged_out_session(self):
        first, second = self.login(), self.login()
        token = first['Cookie'].split('=', 1)[1]
        key = self.auth._session_key(token)
        saved_record = self.auth.client.get(key)
        self.assertIsNotNone(saved_record)

        self.assertEqual(200, self.request('POST', '/api/auth/logout', headers=first)[0])
        self.assertIsNone(self.auth.client.get(key))
        self.auth.client.set(key, saved_record, ex=60)
        self.assertEqual(401, self.request('GET', '/api/auth/me', headers=first)[0])
        self.assertEqual(200, self.request('GET', '/api/auth/me', headers=second)[0])

    def test_denied_execution_has_no_dispatch_and_admin_request_can_enqueue(self):
        record = self.service.create(fixtures.request_contract(workspaceMode='direct',
                                                               allowTests=False, checkTargets=[]))
        task_id = record['taskId']
        path = f'/api/tasks/{task_id}/execution'
        queue = self.service.execution.queue
        member, admin = self.login('member'), self.login()
        denied = (
            ({}, 401, 'SESSION_REQUIRED'),
            (member, 403, 'ADMIN_REQUIRED'),
            ({'Cookie': admin['Cookie']}, 403, 'CSRF_REJECTED'),
        )
        for headers, expected_status, expected_code in denied:
            with self.subTest(error=expected_code):
                status, _, error = self.request('POST', path, {}, headers)
                self.assertEqual((expected_status, expected_code), (status, error['error']['code']))
                self.assertIsNone(queue.get(task_id, fresh=True))
        self.assertEqual(403, self.request('GET', '/api/local-files', headers=member)[0])
        self.assertEqual(202, self.request('POST', path, {}, admin)[0])
        self.assertEqual('queued', queue.get(task_id, fresh=True)['status'])

    def test_invalid_credentials_are_indistinguishable_and_attempts_are_limited(self):
        wrong = self.login_request('operator', 'wrong-password')
        unknown = self.login_request('unknown', 'wrong-password')
        self.assertEqual(401, wrong[0])
        self.assertEqual((401, wrong[2]), (unknown[0], unknown[2]))
        for _ in range(self.failure_limit - 1):
            self.assertEqual(401, self.login_request('operator', 'wrong-password')[0])
        status, _, result = self.login_request('operator')
        self.assertEqual((429, 'LOGIN_RATE_LIMITED'), (status, result['error']['code']))
        self.login('member')

    def test_cli_login_authorized_write_and_logout_share_session_contract(self):
        saved = {}

        def invoke(*args):
            output, error = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = task_cli(['--port', str(self.port), *args])
            return code, json.loads(output.getvalue() or error.getvalue())

        record = self.service.create(fixtures.request_contract(workspaceMode='direct',
                                                               allowTests=False, checkTargets=[]))

        def store_credential(service, key, value):
            saved[service, key] = value

        with (patch('keyring.get_password', side_effect=lambda *key: saved.get(key)),
              patch('keyring.set_password', side_effect=store_credential),
              patch('keyring.delete_password', side_effect=lambda *key: saved.pop(key)),
              patch('repo_agent.cli.account_admin.prompt_password', return_value=TEST_PASSWORD)):
            self.assertEqual(1, invoke('list')[0])
            code, identity = invoke('login', 'operator')
            self.assertEqual(0, code)
            self.assertNotIn('csrfToken', identity)
            self.assertEqual((0, identity), invoke('whoami'))
            self.assertEqual(0, invoke('start', record['taskId'])[0])
            self.assertEqual('queued', self.service.execution.queue.get(record['taskId'], fresh=True)['status'])
            token = next(iter(saved.values()))
            self.assertEqual((0, {'loggedOut': True}), invoke('logout'))
            self.assertFalse(saved)
            self.assertEqual(401, self.request('GET', '/api/auth/me',
                                               headers={'Cookie': f'{cookie_name(self.port)}={token}'})[0])

    def test_cli_vault_native_failure_revokes_unsaved_session(self):
        from keyring.errors import KeyringError

        for error in (OSError('Injected native credential failure'), KeyringError('Injected backend failure')):
            with (self.subTest(error=type(error).__name__),
                  patch('keyring.get_password', return_value=None),
                  patch('keyring.set_password', side_effect=error),
                  patch('repo_agent.cli.account_admin.prompt_password', return_value=TEST_PASSWORD),
                  redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as stderr):
                code = task_cli(['--port', str(self.port), 'login', 'operator'])
                self.assertEqual(1, code)
                self.assertEqual('', output.getvalue())
                self.assertEqual('CLI_CREDENTIAL_STORE_UNAVAILABLE', json.loads(stderr.getvalue())['error']['code'])
                self.assertEqual([], list(self.auth.client.scan_iter(match=self.auth.namespace + ':session:*')))
