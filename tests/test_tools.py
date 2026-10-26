import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.agent import Agent
from repo_agent.decisions import Decision
from repo_agent.tools import RepoTools


class FakeModel:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.requests = []
    def decide(self, messages):
        self.requests.append([message.copy() for message in messages])
        return Decision(**next(self.decisions))


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
        (self.repo / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.tools = RepoTools(self.repo, allow_writes=True, allow_tests=True,
                               test_python=Path(sys.executable))

    def test_browse_search_read(self):
        self.assertIn("calc.py", self.tools.list_files()["files"])
        self.assertTrue(self.tools.search_code("return a - b")["matches"])
        self.assertIn("return a - b", self.tools.read_file("calc.py")["content"])

    def test_prevent_escape(self):
        with self.assertRaises(ValueError):
            self.tools.read_file("../outside.py")
        with self.assertRaises(ValueError):
            self.tools.read_file(str(self.repo.resolve()))

    def test_patch_and_test(self):
        before = self.tools.run_tests("test_calc.py")
        self.assertFalse(before["passed"])
        patch = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
        self.assertTrue(self.tools.apply_patch(patch)["applied"])
        after = self.tools.run_tests("test_calc.py")
        self.assertTrue(after["passed"], after)

    def test_test_environment_overrides_reach_pytest_without_changing_parent(self):
        (self.repo / "test_environment.py").write_text(
            "import os\n\ndef test_environment():\n"
            "    assert os.environ['REPO_AGENT_TEST_MARKER'] == 'child'\n", encoding="utf-8")
        overrides = {"REPO_AGENT_TEST_MARKER": "child"}
        tools = RepoTools(self.repo, allow_tests=True, test_python=Path(sys.executable), test_env=overrides)
        overrides["REPO_AGENT_TEST_MARKER"] = "changed after construction"
        with patch.dict(os.environ, {"REPO_AGENT_TEST_MARKER": "parent"}):
            result = tools.run_tests("test_environment.py")
            self.assertTrue(result["passed"], result)
            self.assertEqual("parent", os.environ["REPO_AGENT_TEST_MARKER"])

    def test_replace_text_generates_an_applicable_patch(self):
        result = self.tools.execute("replace_text", {
            "path": "calc.py", "old_text": "return a - b", "new_text": "return a + b",
        })
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"]["applied"], result)
        self.assertIn("return a + b", (self.repo / "calc.py").read_text(encoding="utf-8"))
        # The returned artifact must describe exactly the edit that reached disk.
        reverse_check = subprocess.run(
            ["git", "-C", str(self.repo), "apply", "--reverse", "--check", "-"],
            input=result["data"]["patch"].encode("utf-8"), capture_output=True,
        )
        self.assertEqual(0, reverse_check.returncode, reverse_check.stderr)

    def test_replace_text_preserves_file_format(self):
        path = self.repo / "sample.py"
        for newline in ("\n", "\r\n"):
            for trailing in ("", newline):
                with self.subTest(newline=repr(newline), trailing=bool(trailing)):
                    original = ("# 中文 café" + newline + "value = 1" + trailing).encode("utf-8")
                    path.write_bytes(original)
                    result = self.tools.replace_text(
                        "sample.py", "# 中文 café\nvalue = 1", "# 中文 café\nvalue = 2")
                    self.assertTrue(result["applied"], result)
                    self.assertEqual(original.replace(b"value = 1", b"value = 2"), path.read_bytes())

    def test_replace_text_rejections_leave_file_unchanged(self):
        path = self.repo / "sample.py"
        cases = [
            ("value = 1\n", "missing", "replacement"),
            ("value = 1\nvalue = 1\n", "value = 1", "value = 2"),
            ("aaa", "aa", "b"),  # overlapping occurrences are also ambiguous
            ("value = 1\n", "", "replacement"),
            ("value = 1\n", "value = 1", "value = 1"),
            ("value = 1\n", None, "replacement"),
            ("value = 1\n", "value = 1", None),
        ]
        for original, old, new in cases:
            with self.subTest(original=original, old=old, new=new):
                path.write_bytes(original.encode("utf-8"))
                result = self.tools.execute("replace_text", {
                    "path": "sample.py", "old_text": old, "new_text": new,
                })
                self.assertFalse(result["ok"], result)
                self.assertEqual(original.encode("utf-8"), path.read_bytes())
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_text("original", encoding="utf-8")
        result = self.tools.execute("replace_text", {
            "path": "../outside.py", "old_text": "original", "new_text": "changed",
        })
        self.assertFalse(result["ok"], result)
        self.assertEqual("original", outside.read_text(encoding="utf-8"))

    def test_replace_text_can_delete_a_block(self):
        result = self.tools.replace_text("calc.py", "    return a - b\n", "")
        self.assertTrue(result["applied"], result)
        self.assertEqual(b"def add(a, b):\n", (self.repo / "calc.py").read_bytes())

    def test_permission_gate(self):
        restricted = RepoTools(self.repo)
        self.assertFalse(restricted.execute("run_tests", {"target": "test_calc.py"})["ok"])
        self.assertFalse(restricted.execute("apply_patch", {"patch": "stuff"})["ok"])
        before = (self.repo / "calc.py").read_bytes()
        self.assertFalse(restricted.execute("replace_text", {
            "path": "calc.py", "old_text": "return a - b", "new_text": "return a + b",
        })["ok"])
        self.assertEqual(before, (self.repo / "calc.py").read_bytes())

    def test_loop_tracks_replace_text_and_invalidates_old_test_result(self):
        for edit_after_test in (False, True):
            with self.subTest(edit_after_test=edit_after_test):
                (self.repo / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
                decisions = [
                    {"action": "replace_text", "args": {
                        "path": "calc.py", "old_text": "return a - b", "new_text": "return a + b"}},
                    {"action": "run_tests", "args": {"target": "test_calc.py"}},
                ]
                if edit_after_test:
                    decisions.append({"action": "replace_text", "args": {
                        "path": "calc.py", "old_text": "return a + b", "new_text": "return a - b"}})
                decisions.append({"action": "finish", "args": {"answer": "Done."}})
                result = Agent(FakeModel(decisions), self.tools,
                               trace_dir=Path(self.tmp.name) / "trace").run("Fix add")
                self.assertEqual("agent_reported_finished", result["status"], result)
                self.assertEqual(not edit_after_test, result["verified_local_change"])
                if edit_after_test:
                    self.assertIsNone(result["last_test_passed"])
                else:
                    self.assertTrue(result["last_test_passed"])

    def test_offline_loop(self):
        model = FakeModel([
            {"action": "search_code", "args": {"query": "def add"}},
            {"action": "read_file", "args": {"path": "calc.py", "start": 1, "end": 3}},
            {"action": "finish", "args": {"answer": "Inspected only; no fix or tests run."}},
        ])
        result = Agent(model, self.tools, trace_dir=Path(self.tmp.name) / "trace").run("Inspect add")
        self.assertEqual("agent_reported_finished", result["status"])
        self.assertIsNone(result["last_test_passed"])
        self.assertFalse(result["verified_local_change"])
        self.assertEqual(2, result["tool_actions"])
        self.assertTrue(Path(result["trace"]).is_file())

    def test_loop_retains_early_evidence_after_six_tool_calls(self):
        decisions = [{"action": "search_code", "args": {"query": "return a - b"}}]
        decisions += [{"action": "read_file", "args": {"path": "calc.py"}} for _ in range(7)]
        decisions.append({"action": "finish", "args": {"answer": "Inspected only."}})
        model = FakeModel(decisions)
        result = Agent(model, self.tools, trace_dir=Path(self.tmp.name) / "trace").run("Inspect add")

        self.assertEqual("agent_reported_finished", result["status"], result)
        final_request = model.requests[-1]
        self.assertEqual(18, len(final_request))  # system + task + eight complete tool exchanges
        self.assertEqual("system", final_request[0]["role"])
        self.assertIn("Coding task:\nInspect add", final_request[1]["content"])
        first_call = final_request[2]["tool_calls"][0]
        self.assertEqual("search_code", first_call["function"]["name"])
        self.assertEqual("tool", final_request[3]["role"])
        self.assertEqual(first_call["id"], final_request[3]["tool_call_id"])
        first_result = json.loads(final_request[3]["content"])
        self.assertIn("return a - b", first_result["data"]["matches"][0]["text"])

    def test_loop_preserves_complete_large_tool_observation(self):
        (self.repo / "long.py").write_text(
            ("# " + "x" * 80 + "\n") * 200 + "# END_OF_OBSERVATION\n", encoding="utf-8")
        model = FakeModel([
            {"action": "read_file", "args": {"path": "long.py", "start": 1, "end": 201}},
            {"action": "finish", "args": {"answer": "Inspected only."}},
        ])
        result = Agent(model, self.tools, trace_dir=Path(self.tmp.name) / "trace").run("Inspect long file")

        self.assertEqual("agent_reported_finished", result["status"], result)
        observation_text = model.requests[-1][-1]["content"]
        self.assertGreater(len(observation_text), 12000)
        observation = json.loads(observation_text)
        self.assertTrue(observation["ok"])
        self.assertTrue(observation["data"]["content"].endswith("201: # END_OF_OBSERVATION"))

    def test_decision_validation(self):
        self.assertEqual("finish", Decision("finish", {"answer": "ok"}).action)
        with self.assertRaises(ValueError):
            Decision("unknown", {})


if __name__ == "__main__":
    unittest.main()
