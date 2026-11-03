"""Offline checks for explicitly starting a registered experiment from HTTP."""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from repo_agent.experiment_runs import ExperimentRuns
from repo_agent.experiments import ExperimentSpec
from repo_agent.round_layout import ID_PATTERN
from repo_agent.task_contract import TaskError
from repo_agent.task_store import TaskStore
from tests import test_tasks as fixtures


class FakeHistory:
    def _registration(self, project, task):
        if (project, task) != ("fixture", "fixture-100"):
            raise TaskError(404, "HISTORY_NOT_FOUND", "not registered")
        return ((task, "100", "tests.fake_experiment"), self.spec, self.root / "runs" / task)


class BlockingRunner:
    def __init__(self, root, release, calls):
        self.root, self.release, self.calls = root, release, calls

    def run_experiment(self, spec, *, on_allocated=None):
        directory = self.root / "runs" / spec.name / "run_20261102-140000_1234abcd"
        directory.mkdir(parents=True)
        (directory / "round.json").write_text(json.dumps({
            "round_id": "20261102-140000_1234abcd",
            "entry": "run",
            "experiment": spec.name,
        }), encoding="utf-8")
        on_allocated(directory, "20261102-140000_1234abcd")
        self.calls.append("started")
        self.release.wait(5)
        self.calls.append("finished")
        return {"status": "independently_verified"}


class ExperimentRunsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source"
        source.mkdir()
        regression = self.root / "regression.patch"
        regression.write_text("", encoding="utf-8")
        self.history = FakeHistory()
        self.history.root = self.root
        self.history.spec = ExperimentSpec(
            name="fixture-100",
            source=source,
            revision="a" * 40,
            test_python=Path(__file__),
            regression_target="test_fixture.py::test_fix",
            failure_contains="failure",
            control_targets=(),
            import_module="fixture",
            regression_patch=regression,
            regression_sha256="b" * 64,
            editable_paths=("fixture.py",),
        )
        self.release = threading.Event()
        self.calls = []
        self.runs = ExperimentRuns(
            self.root,
            TaskStore(self.root / "tasks"),
            self.history,
            runner_factory=lambda spec: BlockingRunner(self.root, self.release, self.calls),
        )

    def test_explicit_start_is_one_at_a_time_and_reports_terminal_state(self):
        status, record = self.runs.start("fixture", "fixture-100", {})
        self.assertEqual(202, status)
        self.assertRegex(record["roundId"], ID_PATTERN.pattern)
        self.assertEqual("running", record["status"])
        self.assertTrue(self.runs.status("fixture", "fixture-100")["active"])
        with self.assertRaises(TaskError) as error:
            self.runs.start("fixture", "fixture-100", {})
        self.assertEqual("EXPERIMENT_BUSY", error.exception.code)
        self.release.set()
        self.runs.threads[0].join(timeout=5)
        status = self.runs.status("fixture", "fixture-100")
        self.assertFalse(status["active"])
        self.assertTrue(status["canStart"])
        self.assertEqual("completed", status["run"]["status"])
        self.assertEqual(["started", "finished"], self.calls)

    def test_start_requires_empty_json_and_registered_identity(self):
        with self.assertRaises(TaskError) as error:
            self.runs.start("fixture", "fixture-100", {"request": "ignored"})
        self.assertEqual("VALIDATION_ERROR", error.exception.code)
        with self.assertRaises(TaskError) as error:
            self.runs.start("other", "fixture-100", {})
        self.assertEqual("HISTORY_NOT_FOUND", error.exception.code)

    def test_other_project_run_is_not_linked_to_current_requirement(self):
        self.runs._active = {"projectId": "other", "taskKey": "other-200",
                             "roundId": "20261102-140000_1234abcd", "status": "running"}
        status = self.runs.status("fixture", "fixture-100")
        self.assertIsNone(status["run"])
        self.assertFalse(status["active"])
        self.assertFalse(status["canStart"])
        self.assertEqual("EXPERIMENT_BUSY", status["startBlockReason"])
        self.runs._active["status"] = "completed"
        status = self.runs.status("fixture", "fixture-100")
        self.assertIsNone(status["run"])
        self.assertTrue(status["canStart"])


class ExperimentRunHTTPTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def test_http_exposes_explicit_post_and_read_only_status(self):
        path = "/api/projects/browser-use/tasks/browser-use-5998/runs"
        record = {"projectId": "browser-use", "taskKey": "browser-use-5998",
                  "roundId": "20261102-140000_1234abcd", "status": "running"}
        status = {"run": None, "active": False, "canStart": True, "liveness": "not_assessed"}
        with patch.object(self.service.experiments, "start", return_value=(202, record)) as start, \
                patch.object(self.service.experiments, "status", return_value=status) as query, \
                fixtures.serving(self.service) as port:
            post = fixtures.http(port, "POST", path, {})
            read = fixtures.http(port, "GET", path + "/status")
            wrong = fixtures.http(port, "GET", path)
        self.assertEqual((202, record), post[:1] + (post[2],))
        self.assertEqual((200, status), read[:1] + (read[2],))
        self.assertEqual("METHOD_NOT_ALLOWED", wrong[2]["error"]["code"])
        start.assert_called_once_with("browser-use", "browser-use-5998", {})
        query.assert_called_once_with("browser-use", "browser-use-5998")


if __name__ == "__main__":
    unittest.main()
