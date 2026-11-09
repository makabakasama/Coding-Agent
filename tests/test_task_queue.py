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
store = DatabaseTaskStore(root/'tasks', root/'runs', root/'workspaces', os.environ['CODING_AGENT_TEST_DATABASE_URL'])
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
                                       os.environ['CODING_AGENT_TEST_DATABASE_URL'])
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
                conn.execute('DELETE FROM task_runs WHERE task_key=%s', (key,))
                conn.execute('DELETE FROM tasks WHERE task_key=%s', (key,))

    def task(self):
        record = self.service.create(fixtures.request_contract(allowTests=False, checkTargets=[]))
        task_id = record['taskId']
        self.keys.append(self.store.round_info(task_id)['taskKey'])
        self.assertEqual('prepared', self.service.prepare(task_id, {})['status'])
        return task_id

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
