import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from repo_agent.agent import Agent, parse_decision
from repo_agent.tools import RepoTools


class FakeModel:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
    def decide(self, messages):
        return json.dumps(next(self.decisions))


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
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

    def test_permission_gate(self):
        restricted = RepoTools(self.repo)
        self.assertFalse(restricted.execute("run_tests", {"target": "test_calc.py"})["ok"])
        self.assertFalse(restricted.execute("apply_patch", {"patch": "stuff"})["ok"])

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

    def test_parser(self):
        self.assertEqual("finish", parse_decision('```json\n{"action":"finish","args":{"answer":"ok"}}\n```')[0])
        with self.assertRaises(ValueError):
            parse_decision("[]")


if __name__ == "__main__":
    unittest.main()
