"""Offline ordinary-task persistence and real local HTTP integration checks."""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from repo_agent.task_contract import TaskError, load_json, validate_contract
from repo_agent.task_http import create_server
from repo_agent.task_projects import TaskProjects
from repo_agent.task_service import TaskService
from repo_agent.task_store import TaskStore


def request_contract(**changes):
    return {"projectId": "sample", "revision": "HEAD", "current": "wrong addition",
            "expected": "correct addition", "allowedPaths": ["calc.py"],
            "allowTests": True, "checkTargets": ["test_calc.py"], **changes}


@contextmanager
def serving(service, frontend=None):
    server = create_server(service, port=0, frontend=frontend)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def http(port, method, path, data=None, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    try:
        connection.request(method, path, body, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        content = response.read()
        value = json.loads(content) if response.getheader("Content-Type", "").startswith("application/json") else content
        return response.status, dict(response.getheaders()), value
    finally:
        connection.close()


class TaskContractTest(unittest.TestCase):
    def test_normalization_and_defaults(self):
        contract = validate_contract(request_contract(current="  first\nsecond  ", allowedPaths=[" calc.py "]))
        self.assertEqual("first\nsecond", contract["current"])
        self.assertEqual(["calc.py"], contract["allowedPaths"])
        self.assertEqual((12, "fix_bug"), (contract["maxSteps"], contract["kind"]))
        self.assertFalse(validate_contract(request_contract(allowTests=False, checkTargets=[]))["allowTests"])

    def test_reject_invalid_fields_types_paths_and_targets(self):
        cases = [
            {"maxSteps": True}, {"maxSteps": 51}, {"maxSteps": 0}, {"kind": "read"},
            {"allowTests": "true"}, {"extra": "ignored?"}, {"projectId": "../x"},
            {"revision": "-option"}, {"revision": "HEAD\n" + "x"}, {"current": "  "},
            {"current": "\ud800"}, {"allowedPaths": []}, {"allowedPaths": "calc.py"},
            {"allowedPaths": [1]}, {"allowedPaths": ["calc.py", " calc.py "]},
            {"allowTests": False}, {"checkTargets": []},
        ]
        paths = ["../x", "/etc/passwd", "C:/x", "a\\b", "a//b", "a/./b", "a/../b",
                 "a*", ".GIT/config", "foo/.ENV.local", "a/venv/x", "foo.", "a/ ", "x\x00", "x\ud800"]
        cases.extend({"allowedPaths": [p]} for p in paths)
        cases.extend({"checkTargets": [t]} for t in ["-q", "C:/test.py", "../test.py", "x.py::", "x.py::a\nb"])
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(TaskError) as error:
                validate_contract(request_contract(**changes))
            self.assertEqual("VALIDATION_ERROR", error.exception.code)
        with self.assertRaises(TaskError):
            validate_contract({})
        with self.assertRaises(TaskError):
            validate_contract([])

    def test_valid_pytest_node_and_duplicate_json_rejection(self):
        validate_contract(request_contract(checkTargets=["tests/test_a.py::TestA::test_x[param]"]))
        for raw in ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}']:
            with self.assertRaises(ValueError):
                load_json(raw)


class TaskIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "calc.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "calc.py")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "baseline")
        self.commit = self.git("rev-parse", "HEAD")
        self.config = self.root / "projects.json"
        self.config.write_text(json.dumps({"projects": [{"projectId": "sample", "name": "Local sample",
                                                        "repo": "repo"}]}), encoding="utf-8")
        self.data = self.root / "tasks"
        self.service = TaskService(TaskStore(self.data), TaskProjects(self.config))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                              text=True, encoding="utf-8", timeout=20, check=True).stdout.strip()

    def test_create_restart_and_branch_movement_preserve_contract(self):
        before_worktrees = self.git("worktree", "list", "--porcelain")
        with patch("urllib.request.urlopen", side_effect=AssertionError("No model API allowed")):
            record = self.service.create(request_contract())
        self.assertEqual(self.commit, record["contract"]["baseCommit"])
        self.assertRegex(record["taskId"], r"^[0-9a-f]{32}$")
        self.assertEqual("created", record["status"])
        self.assertEqual({"task.json"}, {p.name for p in (self.data / f"task_{record['taskId']}").iterdir()})
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "--allow-empty", "-qm", "move HEAD")
        restarted = TaskService(TaskStore(self.data), TaskProjects(self.config))
        self.assertEqual(record, restarted.get(record["taskId"]))
        self.assertEqual(before_worktrees.replace(self.commit, self.git("rev-parse", "HEAD")),
                         self.git("worktree", "list", "--porcelain"))

    def test_project_list_never_returns_local_path(self):
        self.assertEqual([{"projectId": "sample", "name": "Local sample"}], self.service.projects.list())
        self.config.unlink()
        self.assertEqual([], self.service.projects.list())
        self.config.write_text("{broken", encoding="utf-8")
        with self.assertRaises(TaskError) as error:
            self.service.projects.list()
        self.assertEqual("PROJECT_CONFIG_UNAVAILABLE", error.exception.code)

    def test_invalid_request_project_and_revision_do_not_allocate_task(self):
        for request, code in [(request_contract(current=""), "VALIDATION_ERROR"),
                              (request_contract(projectId="missing"), "UNKNOWN_PROJECT"),
                              (request_contract(revision="does-not-exist"), "INVALID_REVISION")]:
            with self.subTest(code=code), self.assertRaises(TaskError) as error:
                self.service.create(request)
            self.assertEqual(code, error.exception.code)
            self.assertFalse(self.data.exists())

    def test_unavailable_repo_does_not_echo_paths(self):
        self.config.write_text(json.dumps({"projects": [{"projectId": "sample", "name": "Sample",
                                                        "repo": "missing-secret-directory"}]}))
        with self.assertRaises(TaskError) as error:
            self.service.create(request_contract())
        self.assertEqual("PROJECT_UNAVAILABLE", error.exception.code)
        self.assertNotIn("missing-secret", json.dumps(error.exception.response()))

    def test_project_subdirectory_does_not_silently_use_parent_repo(self):
        (self.repo / "subdir").mkdir()
        value = {"projects": [{"projectId": "sample", "name": "Sample", "repo": "repo/subdir"}]}
        self.config.write_text(json.dumps(value))
        with self.assertRaises(TaskError) as error:
            self.service.create(request_contract())
        self.assertEqual("PROJECT_UNAVAILABLE", error.exception.code)

    def test_storage_failure_leaves_no_valid_record(self):
        with patch("pathlib.Path.replace", side_effect=OSError("secret-path")), self.assertRaises(TaskError) as error:
            self.service.create(request_contract())
        self.assertEqual("STORAGE_UNAVAILABLE", error.exception.code)
        directories = list(self.data.glob("task_*"))
        self.assertEqual(1, len(directories))
        self.assertTrue((directories[0] / "task.json.tmp").exists())
        with self.assertRaises(TaskError) as missing:
            self.service.get(directories[0].name[5:])
        self.assertEqual("TASK_NOT_FOUND", missing.exception.code)

    def test_bad_id_missing_corrupt_and_io_failure_are_distinct(self):
        for task_id, code in [("../projects.json", "INVALID_TASK_ID"), ("a" * 32, "TASK_NOT_FOUND")]:
            with self.assertRaises(TaskError) as error:
                self.service.get(task_id)
            self.assertEqual(code, error.exception.code)
        record = self.service.create(request_contract())
        path = self.data / f"task_{record['taskId']}" / "task.json"
        original = path.read_bytes()
        for broken in [b"{broken", original.replace(b'"schemaVersion": 1', b'"schemaVersion": true'),
                       original.replace(b'"status": "created"', b'"status": "passed"'),
                       original.replace(record["taskId"].encode(), b"b" * 32),
                       original.replace(b'"maxSteps": 12', b'"maxSteps": 0')]:
            path.write_bytes(broken)
            with self.assertRaises(TaskError) as error:
                self.service.get(record["taskId"])
            self.assertEqual("TASK_RECORD_CORRUPT", error.exception.code)
        with patch("pathlib.Path.open", side_effect=PermissionError("private path")), self.assertRaises(TaskError) as error:
            self.service.get(record["taskId"])
        self.assertEqual("STORAGE_UNAVAILABLE", error.exception.code)

    def test_collision_never_overwrites_old_task(self):
        record = self.service.create(request_contract())
        class FixedUUID:
            hex = record["taskId"]
        with patch("repo_agent.task_store.uuid.uuid4", return_value=FixedUUID()), self.assertRaises(TaskError):
            self.service.create(request_contract(expected="different task"))
        self.assertEqual(record, self.service.get(record["taskId"]))

    def test_symlink_record_is_rejected_when_platform_allows_it(self):
        record = self.service.create(request_contract())
        path = self.data / f"task_{record['taskId']}" / "task.json"
        target = self.root / "external.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("Local Windows account cannot create symbolic links")
        with self.assertRaises(TaskError) as error:
            self.service.get(record["taskId"])
        self.assertEqual("STORAGE_UNAVAILABLE", error.exception.code)

    def test_http_create_read_static_and_restart(self):
        with serving(self.service) as port:
            self.assertEqual(200, http(port, "GET", "/")[0])
            self.assertEqual(200, http(port, "GET", "/app.js")[0])
            status, _, projects = http(port, "GET", "/api/projects")
            ordinary = [{"projectId": p["projectId"], "name": p["name"]} for p in projects["projects"] if p.get("canCreateTask", True)]
            self.assertEqual((200, [{"projectId": "sample", "name": "Local sample"}]), (status, ordinary))
            status, headers, record = http(port, "POST", "/api/tasks", request_contract(),
                                           {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(201, status)
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertEqual("/api/tasks/" + record["taskId"], headers["Location"])
            self.assertEqual(record, http(port, "GET", headers["Location"])[2])
        # A new HTTP server and new service objects read the same disk record.
        service = TaskService(TaskStore(self.data), TaskProjects(self.config))
        with serving(service) as port:
            self.assertEqual(record, http(port, "GET", "/api/tasks/" + record["taskId"])[2])

    def test_http_port_cannot_be_shared_with_another_server(self):
        with serving(self.service) as port:
            with self.assertRaises(OSError):
                create_server(self.service, port=port)

    def test_http_errors_and_no_directory_exposure(self):
        with serving(self.service) as port:
            cases = [
                ("POST", "/api/tasks", b'{"x":1,"x":2}', {}, 400, "INVALID_JSON"),
                ("POST", "/api/tasks", b"\xff", {}, 400, "INVALID_JSON"),
                ("POST", "/api/tasks", b"{}", {"Content-Type": "text/plain"}, 415, "UNSUPPORTED_MEDIA_TYPE"),
                ("POST", "/api/tasks", b"x" * 65537, {}, 413, "REQUEST_TOO_LARGE"),
                ("POST", "/api/tasks", request_contract(), {"Origin": "https://outside.invalid"}, 403, "ORIGIN_REJECTED"),
                ("POST", "/api/tasks", request_contract(), {"Sec-Fetch-Site": "cross-site"}, 403, "ORIGIN_REJECTED"),
                ("GET", "/api/projects", None, {"Host": "outside.invalid"}, 403, "ORIGIN_REJECTED"),
                ("PUT", "/api/tasks", None, {}, 405, "METHOD_NOT_ALLOWED"),
                ("PUT", "/api/projects", None, {}, 405, "METHOD_NOT_ALLOWED"),
                ("GET", "/api/tasks/bad", None, {}, 400, "INVALID_TASK_ID"),
                ("GET", "/api/tasks/" + "a" * 32, None, {}, 404, "TASK_NOT_FOUND"),
                ("GET", "/runs/", None, {}, 404, "ROUTE_NOT_FOUND"),
                ("GET", "/../.env", None, {}, 404, "ROUTE_NOT_FOUND"),
                ("GET", "/README.md", None, {}, 404, "ROUTE_NOT_FOUND"),
                ("GET", "/%2e%2e/.env", None, {}, 404, "ROUTE_NOT_FOUND"),
            ]
            for method, path, data, headers, status, code in cases:
                with self.subTest(path=path, code=code):
                    actual, _, body = http(port, method, path, data, headers)
                    self.assertEqual((status, code), (actual, body["error"]["code"]))
                    self.assertIsInstance(body["error"]["fields"], list)
                    self.assertNotIn(str(self.root), json.dumps(body))
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
