"""M2a tests use only synthetic local Git repositories, no target tests/models."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import test_tasks as fixtures
from tests.test_tasks import request_contract, serving, http
from repo_agent.task_contract import TaskError
from repo_agent.task_projects import TaskProjects
from repo_agent.task_store import TaskStore
from repo_agent.task_service import TaskService
from repo_agent.task_preparation import create_worktree


class PreparationTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def task(self):
        self.service = TaskService(TaskStore(self.data), TaskProjects(self.config), workspace_root=self.root / "workspaces")
        return self.service.create(request_contract())["taskId"]

    def directory(self, task_id):
        return self.data / f"task_{task_id}" / "preparation"

    def test_failed_git_diagnostics_are_saved_and_publicly_readable(self):
        task_id = self.task()
        with patch('repo_agent.task_preparation.create_worktree', side_effect=subprocess.CalledProcessError(
                128, ['git', 'worktree', 'add', str(self.root / 'destination')], output='partial output', stderr='fatal: fixture failure')):
            self.assertEqual('preparation_failed', self.service.prepare(task_id, {})['status'])
        saved = json.loads((self.directory(task_id) / 'diagnostics.json').read_text())
        self.assertEqual('create_worktree', saved['failure']['step'])
        self.assertEqual(128, saved['failure']['exitCode'])
        with serving(self.service) as port:
            status, _, body = http(port, 'GET', f'/api/tasks/{task_id}/preparation/diagnostics')
        self.assertEqual(200, status)
        self.assertIn('fixture failure', body['details']['failure']['stderr'])
        self.assertNotIn(str(self.root), json.dumps(body))
        self.assertEqual('CalledProcessError', body['details']['failure']['errorType'])

    def test_old_missing_diagnostics_do_not_invent_a_reason(self):
        task_id = self.task()
        self.assertEqual({'available': False, 'details': None, 'redacted': True}, self.service.preparation.diagnostics(task_id))

    def test_frozen_commit_repeat_restart_and_m1_unchanged(self):
        task_id = self.task()
        original = self.service.get(task_id)
        self.assertEqual("not_prepared", self.service.preparation.query(task_id)["status"])
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "--allow-empty", "-qm", "move")
        result = self.service.prepare(task_id, {})
        self.assertEqual("prepared", result["status"])
        self.assertEqual(self.commit, result["baseCommit"])
        identity = json.loads((self.directory(task_id) / "identity.json").read_text())
        self.assertEqual(self.commit, identity["head"])
        self.assertNotEqual(identity["gitDir"], identity["commonDir"])
        restarted = TaskService(TaskStore(self.data), TaskProjects(self.config), workspace_root=self.root / "workspaces")
        self.assertEqual(result, restarted.prepare(task_id, {}))
        self.assertEqual(original, restarted.get(task_id))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_concurrent_services_create_only_one(self):
        task_id = self.task()
        entered, release = threading.Event(), threading.Event()
        other = TaskService(TaskStore(self.data), TaskProjects(self.config), workspace_root=self.root / "workspaces")
        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return create_worktree(*args, **kwargs)
        with patch("repo_agent.task_preparation.create_worktree", side_effect=delayed) as create:
            with ThreadPoolExecutor() as pool:
                future = pool.submit(self.service.prepare, task_id, {})
                self.assertTrue(entered.wait(10))
                try:
                    self.assertEqual("preparing", other.prepare(task_id, {})["status"])
                    self.assertEqual("preparing", other.preparation.query(task_id)["status"])
                finally:
                    release.set()
                self.assertEqual("prepared", future.result()["status"])
            self.assertEqual(1, create.call_count)

    def test_failure_preserves_partial_and_never_retries(self):
        task_id = self.task()
        def fail(source, destination, *args, **kwargs):
            destination.mkdir()
            (destination / "partial").write_text("retained")
            raise OSError("private-path")
        with patch("repo_agent.task_preparation.create_worktree", side_effect=fail) as create:
            result = self.service.prepare(task_id, {})
            self.assertEqual("preparation_failed", result["status"])
            self.assertEqual(result, self.service.prepare(task_id, {}))
            self.assertEqual(1, create.call_count)
        identity = json.loads((self.directory(task_id) / "identity.json").read_text())
        self.assertTrue((Path(identity["workspace"]) / "partial").exists())
        self.assertNotIn("private-path", json.dumps(result))

    def test_restart_incomplete_claim_is_failure(self):
        task_id = self.task()
        self.directory(task_id).mkdir()
        result = self.service.preparation.query(task_id)
        self.assertEqual("PREPARATION_INTERRUPTED", result["error"]["code"])
        self.assertEqual(result, self.service.prepare(task_id, {}))

    def test_process_lock_release_marks_interrupted(self):
        task_id = self.task()
        self.directory(task_id).mkdir()
        lock = self.directory(task_id).parent / "preparation.lock"
        script = "from pathlib import Path; from repo_agent.task_preparation import exclusive; import sys; "
        script += "\nwith exclusive(Path(sys.argv[1])) as held:\n print(held, flush=True)\n sys.stdin.readline()\n"
        child = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual("True", child.stdout.readline().strip())
            self.assertEqual("preparing", self.service.preparation.query(task_id)["status"])
        finally:
            child.communicate("release\n", timeout=10)
        self.assertEqual("PREPARATION_INTERRUPTED", self.service.prepare(task_id, {})["error"]["code"])

    def test_checkout_disables_configured_filters_and_hooks(self):
        task_id = self.task()
        (self.repo / ".gitattributes").write_text("calc.py filter=block\n")
        self.git("add", ".gitattributes")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "filter baseline")
        self.git("config", "filter.block.smudge", "command-that-must-not-run")
        self.git("config", "filter.block.required", "true")
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 99\n")
        hook.chmod(0o755)
        task_id = self.service.create(request_contract())["taskId"]
        self.assertEqual("prepared", self.service.prepare(task_id, {})["status"])

    def test_storage_failure_is_not_success_and_claim_is_retained(self):
        task_id = self.task()
        with patch("repo_agent.task_preparation.write_json", side_effect=OSError("private")):
            with self.assertRaises(TaskError) as error:
                self.service.prepare(task_id, {})
        self.assertEqual("STORAGE_UNAVAILABLE", error.exception.code)
        self.assertTrue(self.directory(task_id).exists())
        self.assertEqual("PREPARATION_INTERRUPTED", self.service.prepare(task_id, {})["error"]["code"])

    def test_changed_workspace_identity_is_failure(self):
        task_id = self.task()
        self.service.prepare(task_id, {})
        identity = json.loads((self.directory(task_id) / "identity.json").read_text())
        Path(identity["workspace"], ".git").rename(Path(identity["workspace"], ".git-retained"))
        result = self.service.preparation.query(task_id)
        self.assertEqual("WORKSPACE_IDENTITY_MISMATCH", result["error"]["code"])

    def test_corrupt_record_and_storage_failure(self):
        task_id = self.task()
        self.directory(task_id).mkdir()
        (self.directory(task_id) / "record.json").write_text("{broken")
        with self.assertRaises(TaskError) as error:
            self.service.preparation.query(task_id)
        self.assertEqual("PREPARATION_RECORD_CORRUPT", error.exception.code)

    def test_http_preparation_and_errors(self):
        task_id = self.task()
        path = f"/api/tasks/{task_id}/preparation"
        with serving(self.service) as port:
            self.assertEqual("not_prepared", http(port, "GET", path)[2]["status"])
            self.assertEqual(400, http(port, "POST", path, {"path": "arbitrary"})[0])
            self.assertEqual(403, http(port, "POST", path, {}, {"Origin": "http://evil.invalid"})[0])
            status, _, result = http(port, "POST", path, {})
            self.assertEqual((200, "prepared"), (status, result["status"]))
            self.assertEqual(result, http(port, "GET", path)[2])
            self.assertEqual(405, http(port, "DELETE", path)[0])
            self.assertEqual(404, http(port, "GET", f"/api/tasks/{'a'*32}/preparation")[0])
            self.assertEqual(400, http(port, "GET", "/api/tasks/bad/preparation")[0])


if __name__ == "__main__":
    unittest.main()
