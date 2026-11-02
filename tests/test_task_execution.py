"""Offline M3 integration: injected decisions, synthetic repos and real loopback HTTP."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from tests import test_tasks as fixtures
from tests.test_tasks import request_contract, serving, http
from repo_agent.decisions import Decision
from repo_agent.task_contract import TaskError
from repo_agent.task_projects import TaskProjects
from repo_agent.task_service import TaskService
from repo_agent.task_store import TaskStore


class ScriptedModel:
    def __init__(self, decisions=None):
        self.decisions = list(decisions or [Decision("finish", {"answer": "offline finish"})])

    def decide(self, messages):
        return self.decisions.pop(0)


class ExecutionTest(unittest.TestCase):
    setUpFixture = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def setUp(self):
        self.setUpFixture()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "bug fixture")
        self.service = self.service_for(lambda: ScriptedModel())

    def service_for(self, factory):
        return TaskService(TaskStore(self.data), TaskProjects(self.config), workspace_root=self.root / "workspaces", model_factory=factory)

    def task(self, prepare=True, **changes):
        record = self.service.create(request_contract(**{"checkTargets": ["tests/test_calc.py"], **changes}))
        if prepare:
            self.assertEqual("prepared", self.service.prepare(record["taskId"], {})["status"])
        return record["taskId"]

    def finish(self, task_id):
        for thread in self.service.execution.threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "Offline worker did not finish")
        self.assertEqual("terminated", self.service.execution.get(task_id)["status"])
        return self.service.execution.result(task_id)

    def workspace(self, task_id):
        identity = self.data / f"task_{task_id}/preparation/identity.json"
        return Path(json.loads(identity.read_text())["workspace"])

    def test_full_repair_http_events_result_artifacts_restart_and_duplicate(self):
        class SnapshotRepairModel:
            step = 0
            def decide(self, messages):
                self.step += 1
                if self.step == 1:
                    return Decision("read_file", {"path": "calc.py"})
                if self.step == 2:
                    observation = json.loads(messages[-1]["content"])
                    return Decision("replace_lines", {"snapshot_id": observation["data"]["snapshot_id"],
                                                      "start": 2, "end": 2, "new_text": "    return a + b\n"})
                if self.step == 3:
                    return Decision("run_tests", {"target": "tests/test_calc.py"})
                return Decision("finish", {"answer": "scripted snapshot repair"})
        factory_calls = []
        def factory():
            factory_calls.append(True)
            return SnapshotRepairModel()
        self.service = self.service_for(factory)
        task_id = self.task()
        original = self.service.get(task_id)
        path = f"/api/tasks/{task_id}/execution"
        with patch("urllib.request.urlopen", side_effect=AssertionError("No real model")):
            with serving(self.service) as port:
                self.assertTrue(http(port, "GET", path)[2]["canStart"])
                self.assertEqual(202, http(port, "POST", path, {})[0])
                self.assertEqual(200, http(port, "POST", path, {})[0])
                result = self.finish(task_id)
                self.assertEqual("agent_reported_finished", result["stopReason"])
                self.assertEqual("passed", result["finalChecks"]["status"])
                self.assertEqual({"total": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}, result["finalChecks"]["counts"])
                self.assertEqual("passed", result["review"]["status"])
                self.assertFalse(result["independentlyVerified"])
                self.assertEqual("injected_test_model", result["source"])
                self.assertEqual(result, http(port, "GET", path + "/result")[2])
                first = http(port, "GET", path + "/events?after=0&limit=2")[2]
                self.assertEqual([1, 2], [r["seq"] for r in first["events"]])
                self.assertTrue(first["hasMore"])
                rest = http(port, "GET", path + "/events?after=2&limit=100")[2]
                feedback = [e["feedback"] for e in rest["events"] if e["type"] == "observation"]
                self.assertEqual("calc.py", feedback[0]["path"])
                test_feedback = next(e for e in feedback if e["kind"] == "run_tests")
                self.assertEqual((True, 0, 1), (test_feedback["passed"], test_feedback["exitCode"], test_feedback["counts"]["passed"]))
                diff = http(port, "GET", path + "/artifacts/diff?offset=0&limit=16000")[2]
                self.assertIn("return a + b", diff["content"])
                self.assertTrue(diff["redacted"])
                page = http(port, "GET", path + "/artifacts/diff?limit=8")[2]
                self.assertEqual(8, page["nextOffset"])
                self.assertTrue(page["hasMore"])
                self.assertEqual(404, http(port, "GET", path + "/artifacts/runtime.json")[0])
                self.assertEqual(404, http(port, "GET", path + "/artifacts/%2e%2e%2f.env")[0])
                self.assertNotIn(str(self.root), json.dumps(result) + json.dumps(rest) + json.dumps(diff))
        self.assertEqual(1, len(factory_calls))
        patch_file = self.data / f"task_{task_id}/execution/candidate.patch"
        import hashlib
        self.assertEqual(hashlib.sha256(patch_file.read_bytes()).hexdigest(), result["patchExport"]["sha256"])
        self.assertEqual(original, self.service.get(task_id))
        restart = self.service_for(factory)
        self.assertEqual("terminated", restart.execution.get(task_id)["status"])
        self.assertEqual(200, restart.execution.start(task_id, {})[0])
        self.assertEqual(result, restart.execution.result(task_id))
        self.assertEqual(1, len(factory_calls))

    def test_async_global_exclusion_and_same_task_duplicate(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingModel:
            def decide(self, messages):
                entered.set()
                if not release.wait(15):
                    raise RuntimeError("test wait exceeded")
                return Decision("finish", {"answer": "done"})
        self.service = self.service_for(BlockingModel)
        task_id, other = self.task(), self.task()
        self.assertEqual(202, self.service.execution.start(task_id, {})[0])
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual("running", self.service.execution.get(task_id)["status"])
            restarted = self.service_for(BlockingModel)
            self.assertEqual(200, restarted.execution.start(task_id, {})[0])
            self.assertEqual("EXECUTION_BUSY", restarted.execution.get(other)["startBlockReason"])
            with self.assertRaises(TaskError) as error:
                restarted.execution.start(other, {})
            self.assertEqual("EXECUTION_BUSY", error.exception.code)
        finally:
            release.set()
        self.finish(task_id)

    def test_preparation_dirty_identity_config_and_http_input_rejections(self):
        task_id = self.task(prepare=False)
        path = f"/api/tasks/{task_id}/execution"
        with serving(self.service) as port:
            for suffix in ("?unknown=1", "/events?after=-1", "/events?limit=101", "/events?after=0&after=1"):
                self.assertEqual(400, http(port, "GET", path + suffix)[0])
            self.assertEqual(400, http(port, "POST", path, {"model": "browser-choice"})[0])
            self.assertEqual(409, http(port, "POST", path, {})[0])
            self.assertEqual(409, http(port, "GET", path + "/result")[0])
            self.assertEqual(403, http(port, "POST", path, {}, {"Origin": "http://evil.invalid"})[0])
            self.assertEqual(405, http(port, "DELETE", path)[0])
        self.service.prepare(task_id, {})
        workspace = self.workspace(task_id)
        (workspace / "calc.py").write_text("dirty")
        self.assertEqual("WORKSPACE_DIRTY", self.service.execution.get(task_id)["startBlockReason"])
        (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        config = json.loads(self.config.read_text())
        config["projects"][0]["execution"] = {"testEnv": {"API_KEY": "never"}}
        self.config.write_text(json.dumps(config))
        self.assertEqual("EXECUTION_CONFIG_UNAVAILABLE", self.service.execution.get(task_id)["startBlockReason"])
        Path(workspace / ".git").rename(workspace / ".git-retained")
        self.assertFalse(self.service.execution.get(task_id)["canStart"])

    def test_restart_unknown_permanent_claim_and_unresolved_global_owner(self):
        task_id, other = self.task(), self.task()
        directory = self.data / f"task_{task_id}/execution"
        directory.mkdir()
        owner = {"taskId": task_id, "executionId": "a" * 32}
        (self.data / "execution-owner.json").write_text(json.dumps(owner))
        self.assertEqual("unknown", self.service.execution.get(task_id)["status"])
        self.assertEqual(200, self.service.execution.start(task_id, {})[0])
        self.assertEqual("EXECUTION_UNRESOLVED", self.service.execution.get(other)["startBlockReason"])
        with self.assertRaises(TaskError):
            self.service.execution.start(other, {})

    def test_tool_scope_and_frozen_targets_are_enforced(self):
        patch_text = "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -1 +1 @@\n-from calc import add\n+pass\n"
        decisions = [Decision("replace_text", {"path": "tests/test_calc.py", "old_text": "assert add(2, 3) == 5", "new_text": "assert True"}),
                     Decision("apply_patch", {"patch": patch_text}), Decision("run_tests", {"target": "."}),
                     Decision("replace_text", {"path": "other.py", "old_text": "value = 1", "new_text": "value = 2"}),
                     Decision("finish", {"answer": "attempts blocked"})]
        (self.repo / "other.py").write_text("value = 1\n")
        self.git("add", ".")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "outside file")
        self.service = self.service_for(lambda: ScriptedModel(decisions))
        task_id = self.task()
        self.service.execution.start(task_id, {})
        result = self.finish(task_id)
        self.assertEqual([], result["review"]["changedPaths"])
        observations = [r for r in self.service.execution.events(task_id, limit=100)["events"] if r["type"] == "observation"]
        self.assertEqual([False] * 4, [r["ok"] for r in observations])
        self.assertEqual("failed", result["finalChecks"]["status"])

    def test_tests_not_authorized_are_skipped(self):
        task_id = self.task(allowTests=False, checkTargets=[])
        self.service.execution.start(task_id, {})
        result = self.finish(task_id)
        self.assertEqual("skipped", result["finalChecks"]["status"])
        self.assertIsNone(result["finalChecks"]["counts"])

    def test_all_skipped_and_mixed_counts(self):
        (self.repo / "tests/test_calc.py").write_text("import pytest\n@pytest.mark.skip(reason='fixture')\ndef test_skip():\n    pass\n")
        self.git("add", ".")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "skip fixture")
        task_id = self.task()
        self.service.execution.start(task_id, {})
        result = self.finish(task_id)
        self.assertEqual("skipped", result["finalChecks"]["status"])
        self.assertEqual(1, result["finalChecks"]["counts"]["skipped"])
        self.assertEqual(0, result["finalChecks"]["counts"]["passed"])
        (self.repo / "tests/test_calc.py").write_text("import pytest\n@pytest.mark.skip(reason='fixture')\ndef test_skip():\n    pass\ndef test_pass():\n    assert True\n")
        self.git("add", ".")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "mixed fixture")
        second = self.task()
        self.service.execution.start(second, {})
        result = self.finish(second)
        self.assertEqual("passed", result["finalChecks"]["status"])
        self.assertEqual((1, 1), (result["finalChecks"]["counts"]["passed"], result["finalChecks"]["counts"]["skipped"]))

    def test_step_limit_and_model_error_keep_materials(self):
        self.service = self.service_for(lambda: ScriptedModel([Decision("read_file", {"path": "calc.py"})]))
        task_id = self.task(maxSteps=1)
        self.service.execution.start(task_id, {})
        result = self.finish(task_id)
        self.assertEqual("step_limit", result["stopReason"])
        self.assertTrue(any(a["artifactId"] == "diff" for a in result["artifacts"]))
        class Broken:
            def decide(self, messages):
                raise RuntimeError("synthetic model failure")
        self.service = self.service_for(Broken)
        second = self.task()
        self.service.execution.start(second, {})
        result = self.finish(second)
        self.assertEqual("agent_error", result["stopReason"])
        self.assertEqual("error", result["agent"]["status"])

    def test_test_side_effects_are_audited_and_stop_remaining_checks(self):
        (self.repo / "tests/test_calc.py").write_text("from pathlib import Path\ndef test_side_effect():\n    Path('outside.txt').write_text('retained')\n    assert True\n")
        (self.repo / "tests/test_other.py").write_text("def test_other():\n    assert True\n")
        self.git("add", ".")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "side effect fixture")
        task_id = self.task(checkTargets=["tests/test_calc.py", "tests/test_other.py"])
        self.service.execution.start(task_id, {})
        result = self.finish(task_id)
        self.assertEqual("policy_violation", result["stopReason"])
        self.assertEqual("failed", result["review"]["status"])
        self.assertIn("outside.txt", result["review"]["outOfScopePaths"])
        self.assertEqual("not_run", result["finalChecks"]["checks"][1]["status"])
        self.assertTrue((self.workspace(task_id) / "outside.txt").exists())

    def test_known_secret_rejects_raw_patch_export_and_redacts_trace(self):
        token = "synthetic-secret-for-m3-test-918273"
        decisions = [Decision("replace_text", {"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b  # " + token}),
                     Decision("finish", {"answer": "secret " + token})]
        self.service = self.service_for(lambda: ScriptedModel(decisions))
        task_id = self.task(allowTests=False, checkTargets=[])
        with patch.dict("os.environ", {"SYNTHETIC_API_KEY": token}):
            self.service.execution.start(task_id, {})
            result = self.finish(task_id)
        self.assertEqual("not_exported", result["patchExport"]["status"])
        directory = self.data / f"task_{task_id}/execution"
        self.assertFalse((directory / "candidate.patch").exists())
        self.assertNotIn(token, json.dumps(result))
        for path in (directory / "trace").glob("*.jsonl"):
            self.assertNotIn(token, path.read_text(encoding="utf-8"))
        self.assertNotIn(token, self.service.execution.artifact(task_id, "diff")["content"])

    def test_no_tests_collection_error_and_unavailable_counts_not_passed(self):
        for source in ("value = 1\n", "import missing_synthetic_module_982734\n"):
            with self.subTest(source=source):
                (self.repo / "tests/test_calc.py").write_text(source)
                self.git("add", ".")
                self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "collection fixture")
                task_id = self.task()
                self.service.execution.start(task_id, {})
                result = self.finish(task_id)
                self.assertEqual("failed", result["finalChecks"]["status"])
        task_id = self.task()
        with patch("repo_agent.task_execution_tools.ET.fromstring", side_effect=ValueError("corrupt fixture")):
            self.service.execution.start(task_id, {})
            result = self.finish(task_id)
        self.assertIsNone(result["finalChecks"]["counts"])
        self.assertEqual("failed", result["finalChecks"]["status"])

    def test_factory_exception_and_result_storage_failure_are_retained(self):
        def broken_factory():
            raise RuntimeError("factory fixture")
        self.service = self.service_for(broken_factory)
        task_id = self.task(allowTests=False, checkTargets=[])
        self.service.execution.start(task_id, {})
        result = self.finish(task_id)
        self.assertEqual("execution_error", result["stopReason"])
        self.assertEqual("not_run", result["finalChecks"]["status"])
        self.service = self.service_for(lambda: ScriptedModel())
        second = self.task(allowTests=False, checkTargets=[])
        from repo_agent.task_execution import write_json
        def failing_write(path, value):
            if path.name == "result.json":
                raise OSError("synthetic disk error")
            return write_json(path, value)
        with patch("repo_agent.task_execution.write_json", side_effect=failing_write):
            self.service.execution.start(second, {})
            for thread in self.service.execution.threads:
                thread.join(15)
        self.assertEqual("unknown", self.service.execution.get(second)["status"])
        self.assertEqual(200, self.service.execution.start(second, {})[0])
        self.assertTrue((self.data / f"task_{second}/execution/events").exists())


if __name__ == "__main__":
    unittest.main()
