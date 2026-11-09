"""Real PostgreSQL and independent processes, with injected local Agent decisions."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.schemas.task_contract import TaskError
from tests import test_tasks as fixtures
from tests.test_task_execution import ScriptedModel


# This helper is executed in a separate OS process. It never constructs a real provider.
WORKER_SCRIPT = r'''
import json, os, sys, threading, time
from pathlib import Path
from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.services.worker_pool import WorkerPool
from repo_agent.core.decisions import Decision
root = Path(sys.argv[1])
class LocalModel:
    def __init__(self):
        self.edited = False
    def decide(self, messages):
        if not self.edited:
            self.edited = True
            return Decision('replace_text', {'path':'calc.py','old_text':'value = 1','new_text':'value = 2'})
        while not (root / 'release').exists():
            time.sleep(.05)
        return Decision('finish', {'answer':'injected local result'})
store = DatabaseTaskStore(root/'tasks', root/'runs', root/'workspaces', os.environ['CODING_AGENT_TEST_DATABASE_URL'],redis_url=os.environ.get('CODING_AGENT_TEST_REDIS_URL'))
service = TaskService(store, TaskProjects(root/'projects.json'), workspace_root=root/'legacy',
                      project_root=root, model_factory=LocalModel)
stop = threading.Event()
def watch():
    while not (root/'stop').exists():
        time.sleep(.05)
    stop.set()
threading.Thread(target=watch,daemon=True).start()
WorkerPool(service.execution.queue,service.execution.execution,workers=3,poll_seconds=.05).run(stop)
'''


@unittest.skipUnless(os.environ.get('CODING_AGENT_TEST_DATABASE_URL'), 'Dedicated test database not configured')
class QueueIntegrationTest(unittest.TestCase):
    git = fixtures.TaskIntegrationTest.git

    def setUp(self):
        fixtures.TaskIntegrationTest.setUp(self)
        self.keys, self.processes = [], []
        self.store = DatabaseTaskStore(self.data, self.root/'runs', self.root/'workspaces',
                                       os.environ['CODING_AGENT_TEST_DATABASE_URL'],redis_url=os.environ.get('CODING_AGENT_TEST_REDIS_URL'))
        self.addCleanup(self.store.close)
        self.service = self.service_for()
        self.queue = self.service.execution.queue
        self.addCleanup(self.cleanup_queue)

    def service_for(self):
        return TaskService(self.store, TaskProjects(self.config), workspace_root=self.root/'legacy',
                           project_root=self.root, model_factory=ScriptedModel)

    def cleanup_queue(self):
        (self.root/'release').touch()
        (self.root/'stop').touch()
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            process.stdout.close()
        with self.store._connection() as conn:
            for key in self.keys:
                conn.execute('DELETE FROM task_trash WHERE task_id IN (SELECT task_id FROM task_runs WHERE task_key=%s)', (key,))
                conn.execute('DELETE FROM task_runs WHERE task_key=%s', (key,))
                conn.execute('DELETE FROM tasks WHERE task_key=%s', (key,))

    def task(self, workspace_mode=None):
        options = {'workspaceMode': workspace_mode} if workspace_mode else {}
        record = self.service.create(fixtures.request_contract(allowTests=False, checkTargets=[], **options))
        task_id = record['taskId']
        self.keys.append(self.store.round_info(task_id)['taskKey'])
        if workspace_mode != 'direct':
            self.assertEqual('prepared', self.service.prepare(task_id, {})['status'])
        return task_id

    def test_trash_cancels_pending_dispatch_and_restore_requires_explicit_enqueue(self):
        task_id = self.task('direct')
        self.service.execution.start(task_id, {})
        self.service.lifecycle.change(task_id, {'deleted': True})
        self.assertIsNone(self.queue.claim('injected-worker'))
        self.assertIsNone(self.queue.get(task_id, fresh=True))
        self.assertEqual([], self.service.list())
        key = self.store.round_info(task_id)['taskKey']
        self.assertEqual([], self.store.rounds('sample', key))
        self.assertIn(key, self.store.hidden_requirements('sample'))
        self.assertEqual(task_id, self.service_for().list(deleted=True)[0]['taskId'])
        with self.assertRaises(fixtures.TaskError) as error:
            self.queue.enqueue(task_id)
        self.assertEqual('TASK_DELETED', error.exception.code)
        self.service.lifecycle.change(task_id, {'deleted': False})
        self.assertNotIn(key, self.store.hidden_requirements('sample'))
        self.assertIsNone(self.queue.claim('injected-worker'))
        self.assertEqual(202, self.service.execution.start(task_id, {})[0])

    def test_running_and_unconfirmed_dispatch_cannot_be_deleted(self):
        task_id = self.task('direct')
        self.service.execution.start(task_id, {})
        self.assertEqual(task_id, self.queue.claim('injected-worker')[0])
        for state in ('running', 'unknown'):
            with self.subTest(state=state), self.assertRaises(fixtures.TaskError) as error:
                self.service.lifecycle.change(task_id, {'deleted': True})
            self.assertEqual('TASK_BUSY', error.exception.code)
            self.assertNotIn(task_id, self.store.deleted_ids())
            self.queue.finish(task_id, 'injected-worker', terminated=False, reason='INJECTED_INTERRUPTION')

    def test_permanent_delete_removes_only_selected_round_and_cannot_be_rematerialized(self):
        task_id = self.task('direct')
        info = self.store.round_info(task_id)
        key = info['taskKey']
        other = self.service.new_round('sample', key, {})['taskId']
        directory = self.store.directory(task_id)
        row = self.store._run(task_id)
        relative = directory.relative_to(self.root).as_posix()
        with self.store._connection() as conn:
            conn.execute('INSERT INTO historical_materials(path,sha256,content) VALUES (%s,%s,%s)',
                         (relative+'/private-log.txt', 'synthetic', b'synthetic evidence'))
        self.service.lifecycle.change(task_id, {'deleted': True})
        self.service.list(deleted=True)
        self.assertTrue(self.service.purge.apply(task_id, {'confirmTaskId': task_id})['purged'])
        with self.store._connection() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM task_runs WHERE task_id=%s', (task_id,)).fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM historical_materials WHERE path=%s',
                                           (relative+'/private-log.txt',)).fetchone())
        self.assertFalse(directory.exists())
        for action in (lambda: self.store.get(task_id), lambda: self.store._materialize(row)):
            with self.assertRaises(fixtures.TaskError) as error:
                action()
            self.assertEqual(410, error.exception.status)
        self.assertFalse(directory.exists())
        self.assertEqual(other, self.service.list()[0]['taskId'])
        self.assertEqual([], self.service.list(deleted=True))
        self.service.lifecycle.change(other, {'deleted': True})
        self.service.purge.apply(other, {'confirmTaskId': other})
        with self.store._connection() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM tasks WHERE task_key=%s', (key,)).fetchone())
        self.assertFalse((directory.parent/'task.json').exists())

    def test_database_failure_after_file_cleanup_is_retryable(self):
        from unittest.mock import patch
        task_id = self.task('direct')
        self.service.lifecycle.change(task_id, {'deleted': True})
        def fail_commit(task_id, cleanup, **kwargs):
            cleanup(True)
            raise fixtures.TaskError(503, 'DATABASE_UNAVAILABLE', 'Injected commit failure')
        with patch.object(self.store, 'purge_run', side_effect=fail_commit):
            with self.assertRaises(fixtures.TaskError):
                self.service.purge.apply(task_id, {'confirmTaskId': task_id})
        self.assertEqual('purging', self.service.purge.get(task_id)['status'])
        self.assertTrue(self.service.list(deleted=True)[0]['purgePending'])
        self.assertTrue(self.service.purge.apply(task_id, {'confirmTaskId': task_id})['purged'])

    def test_claim_and_trash_race_has_one_winner_and_preserves_running_records(self):
        task_id = self.task('direct')
        self.service.execution.start(task_id, {})
        def delete():
            try:
                self.service.lifecycle.change(task_id, {'deleted': True})
                return 'deleted'
            except fixtures.TaskError as exc:
                self.assertEqual('TASK_BUSY', exc.code)
                return 'busy'
        with ThreadPoolExecutor(max_workers=2) as clients:
            deleting = clients.submit(delete)
            claiming = clients.submit(self.queue.claim, 'injected-worker')
            result, claim = deleting.result(), claiming.result()
        if result == 'deleted':
            self.assertIsNone(claim)
            self.assertIsNone(self.queue.get(task_id, fresh=True))
        else:
            self.assertEqual(task_id, claim[0])
            self.assertNotIn(task_id, self.store.deleted_ids())
            self.assertEqual('running', self.queue.get(task_id, fresh=True)['status'])

    def worker(self):
        process = subprocess.Popen([sys.executable, '-c', WORKER_SCRIPT, str(self.root)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process

    def wait_for(self, check, process=None):
        deadline = time.monotonic()+35
        while time.monotonic() < deadline:
            if check():
                return
            if process is not None and process.poll() is not None:
                self.fail('Worker exited: '+process.stdout.read().decode(errors='replace'))
            time.sleep(.05)
        self.fail('Timed out waiting for local queue condition')

    def workspace(self, task_id):
        path = self.store.directory(task_id)/'preparation/identity.json'
        return Path(json.loads(path.read_text())['workspace'])

    def test_http_enqueue_restart_and_three_slot_independent_pool(self):
        ids = [self.task() for _ in range(4)]
        with fixtures.serving(self.service) as port:
            def submit(_):
                return fixtures.http(port, 'POST', f'/api/tasks/{ids[0]}/execution', {})
            with ThreadPoolExecutor(max_workers=6) as clients:
                responses = list(clients.map(submit, range(6)))
            self.assertTrue(all(r[0] in (200,202) for r in responses))
            self.assertEqual(1,len({r[2]['executionId'] for r in responses}))
            for task_id in ids[1:]:
                self.assertEqual(202, fixtures.http(port,'POST',f'/api/tasks/{task_id}/execution',{})[0])
        restarted = self.service_for()
        self.assertTrue(all(restarted.execution.get(t)['status']=='queued' for t in ids))
        self.assertEqual([], self.service.execution.threads)
        process = self.worker()
        self.wait_for(lambda: sum(self.queue.get(t)['status']=='running' for t in ids)==3, process)
        self.wait_for(lambda: sum((self.workspace(t)/'calc.py').read_text().strip()=='value = 2' for t in ids)==3, process)
        self.assertEqual(1, sum(self.queue.get(t)['status']=='queued' for t in ids))
        # Start and stop an API while the separate Worker remains active.
        with fixtures.serving(restarted) as port:
            self.assertEqual(200,fixtures.http(port,'GET',f'/api/tasks/{ids[0]}/execution')[0])
        self.assertIsNone(process.poll())
        (self.root/'release').touch()
        self.wait_for(lambda: all(self.queue.get(t)['status']=='terminated' for t in ids), process)
        for task_id in ids:
            result = restarted.execution.result(task_id)
            self.assertEqual(['calc.py'],result['review']['changedPaths'])
            self.assertEqual('skipped',result['finalChecks']['status'])
            self.assertEqual('agent_reported_finished',result['stopReason'])
            self.assertFalse(result['independentlyVerified'])
        (self.root/'stop').touch()
        self.assertEqual(0,process.wait(timeout=15))

    def test_killed_worker_keeps_evidence_and_never_requeues_claim(self):
        task_id = self.task()
        self.service.execution.start(task_id,{})
        process = self.worker()
        self.wait_for(lambda: (self.workspace(task_id)/'calc.py').read_text().strip()=='value = 2', process)
        process.kill()
        process.wait(timeout=10)
        state = self.service.execution.execution._read_state(task_id)
        self.assertNotEqual('terminated',state['status'])
        # Restarting the pool confirms old claims as unknown instead of replaying them.
        replacement = self.worker()
        self.wait_for(lambda:self.queue.get(task_id)['status']=='unknown', replacement)
        queried = self.service.execution.get(task_id)
        self.assertEqual('unknown',queried['status'])
        self.assertEqual('WORKER_INTERRUPTED',queried['stopReason'])
        self.assertFalse(queried['canStart'])
        self.assertEqual('value = 2',(self.workspace(task_id)/'calc.py').read_text().strip())
        self.assertTrue(list((self.store.directory(task_id)/'execution/events').glob('*.json')))
        self.assertEqual(200,self.service.execution.start(task_id,{})[0])
        self.assertIsNone(self.queue.claim('other-worker'))
        (self.root/'stop').touch()
        self.assertEqual(0,replacement.wait(timeout=15))

    def test_expired_heartbeat_and_singleton_pool_lock(self):
        task_id = self.task()
        self.queue.enqueue(task_id)
        with self.queue.pool_session():
            with self.assertRaises(TaskError) as error:
                with self.queue.pool_session():
                    self.fail('Second pool acquired the session lock')
            self.assertEqual('WORKER_ALREADY_RUNNING', error.exception.code)
        self.queue.claim('lost-worker')
        with self.store._connection() as conn:
            conn.execute("UPDATE task_dispatch SET heartbeat_at=now()-interval '30 seconds' WHERE task_id=%s",(task_id,))
        self.assertEqual('unknown',self.service.execution.get(task_id)['status'])
        self.assertIsNone(self.queue.claim('replacement'))


    def test_direct_tasks_wait_for_same_repository_while_other_repositories_can_be_claimed(self):
        first, second, other = self.task('direct'), self.task('direct'), self.task()
        self.service.execution.start(first,{})
        self.service.execution.start(second,{})
        self.queue.enqueue(other, repository_key='independent-repository')
        self.assertEqual(first,self.queue.claim('worker')[0])
        self.assertEqual(other,self.queue.claim('worker')[0])
        self.assertIsNone(self.queue.claim('worker'))
        self.assertEqual('queued', self.queue.get(second)['status'])
        self.queue.finish(first,'worker',terminated=True,reason='test')
        self.assertEqual(second,self.queue.claim('worker')[0])

    def test_undo_excludes_an_active_task_in_the_same_repository(self):
        from repo_agent.core.decisions import Decision
        from repo_agent.backend.schemas.task_contract import TaskError
        first, second = self.task('direct'), self.task('direct')
        self.service.execution.execution.model_factory = lambda: ScriptedModel([
            Decision('replace_text',{'path':'calc.py','old_text':'value = 1','new_text':'value = 2'}),
            Decision('finish',{'answer':'changed'})])
        self.service.execution.start(first,{})
        claim = self.queue.claim('worker')
        self.service.execution.execution.run_claimed(*claim)
        self.queue.finish(first,'worker',terminated=True,reason='agent_reported_finished')
        self.service.execution.start(second,{})
        self.assertEqual(second,self.queue.claim('worker')[0])
        with self.assertRaises(TaskError) as error:
            self.service.undo.apply(first,{'executionId':claim[1]})
        self.assertEqual('REPOSITORY_BUSY',error.exception.code)
        self.assertEqual('value = 2',(self.repo/'calc.py').read_text().strip())
        self.queue.finish(second,'worker',terminated=False,reason='test')
        self.assertEqual('undone',self.service.undo.apply(first,{'executionId':claim[1]})['status'])


    def test_redis_hits_and_cross_instance_dispatch_invalidation(self):
        if self.store.cache is None:
            self.skipTest('Real Redis test connection not configured')
        from unittest.mock import patch
        from repo_agent.backend.repositories.task_queue import TaskQueue
        task_id = self.task()
        self.store.invalidate_run(task_id)
        with patch.object(self.store,'_connect',wraps=self.store._connect) as connect:
            self.store.get(task_id)
            self.store.directory(task_id)
            self.store.get(task_id)
        self.assertEqual(1,connect.call_count)
        self.queue.enqueue(task_id)
        self.assertEqual('queued',self.queue.get(task_id)['status'])
        other_store=DatabaseTaskStore(self.data,self.root/'runs',self.root/'workspaces',
            os.environ['CODING_AGENT_TEST_DATABASE_URL'],redis_url=os.environ['CODING_AGENT_TEST_REDIS_URL'])
        self.addCleanup(other_store.close)
        other=TaskQueue(other_store)
        self.assertEqual(task_id,other.claim('worker')[0])
        self.assertEqual('running',self.queue.get(task_id)['status'])
        self.assertEqual('running',self.queue.get_many([task_id])[task_id]['status'])
        other.finish(task_id,'worker',terminated=True,reason='test')
        self.assertEqual('terminated',self.queue.get(task_id)['status'])
        with self.store._connection() as conn:
            conn.execute("UPDATE task_dispatch SET status='unknown' WHERE task_id=%s",(task_id,))
        self.assertEqual('unknown',self.queue.get(task_id,fresh=True)['status'])

    def test_list_cache_hit_avoids_database_and_files_and_writes_invalidate_across_instances(self):
        if self.store.cache is None:
            self.skipTest('Real Redis test connection not configured')
        from unittest.mock import patch
        task_id = self.task('direct')
        first = self.service.list()
        with patch.object(self.store, '_connect', side_effect=AssertionError('Unexpected database read')):
            with patch.object(self.store, '_materialize', side_effect=AssertionError('Unexpected material read')):
                self.assertEqual(first, self.service.list())
        other_store = DatabaseTaskStore(self.data, self.root/'runs', self.root/'workspaces',
            os.environ['CODING_AGENT_TEST_DATABASE_URL'], redis_url=os.environ['CODING_AGENT_TEST_REDIS_URL'])
        self.addCleanup(other_store.close)
        other = TaskService(other_store, TaskProjects(self.config), project_root=self.root, model_factory=ScriptedModel)
        self.assertEqual(first, other.list())
        other.lifecycle.change(task_id, {'deleted': True})
        self.assertEqual([], self.service.list())
        self.assertEqual(task_id, self.service.list(deleted=True)[0]['taskId'])
        other.lifecycle.change(task_id, {'deleted': False})
        self.assertEqual(task_id, self.service.list()[0]['taskId'])
        self.assertEqual([], self.service.list(deleted=True))
        other.execution.start(task_id, {})
        self.assertEqual('queued', self.service.list()[0]['executionStatus'])
        other.execution.queue.claim('injected-worker')
        self.assertEqual('running', self.service.list()[0]['executionStatus'])
        other.execution.queue.finish(task_id, 'injected-worker', terminated=False, reason='INJECTED_INTERRUPTION')
        self.assertEqual('unknown', self.service.list()[0]['executionStatus'])

    def test_simple_file_submission_runs_without_git_or_manual_preparation(self):
        folder=self.root/'plain';folder.mkdir()
        target=folder/'calc.py';target.write_text('value = 1\n',encoding='utf-8')
        with fixtures.serving(self.service) as port:
            status,_,record=fixtures.http(port,'POST','/api/tasks',
                                         {'targetPath':str(target),'instruction':'Change value to 2'})
            self.assertEqual(201,status)
            self.assertIsNone(record['contract']['baseCommit'])
            task_id=record['taskId']
            self.keys.append(self.store.round_info(task_id)['taskKey'])
            self.assertFalse((self.store.directory(task_id)/'preparation').exists())
            self.assertEqual(202,fixtures.http(port,'POST',f'/api/tasks/{task_id}/execution',{})[0])
        (self.root/'release').touch()
        process=self.worker()
        self.wait_for(lambda:self.queue.get(task_id)['status']=='terminated',process)
        self.assertEqual('value = 2',target.read_text().strip())
        self.assertFalse((folder/'.git').exists())
        self.assertTrue(self.service.undo.get(task_id)['canUndo'])
        (self.root/'stop').touch()
        self.assertEqual(0,process.wait(timeout=15))
