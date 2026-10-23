import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from examples.live_llm_smoke import run_smoke
from repo_agent.agent import parse_decision
from repo_agent.workspace import create_worktree


class WorkspaceTest(unittest.TestCase):
    def test_fixed_commit_and_independent_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(source), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            (source / "sample.txt").write_text("base", encoding="utf-8")
            git("add", "sample.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "fixture")
            commit = git("rev-parse", "HEAD")
            destination = Path(directory) / "workspace"
            result = create_worktree(source, destination, commit)
            try:
                self.assertEqual(commit, result["base_commit"])
                (destination / "sample.txt").write_text("changed", encoding="utf-8")
                self.assertEqual("base", (source / "sample.txt").read_text(encoding="utf-8"))
                with self.assertRaises(ValueError):
                    create_worktree(source, destination, commit)
            finally:
                git("worktree", "remove", "--force", str(destination))

    def test_finish_rejects_nonobject_args(self):
        for args in (None, [], "invalid"):
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_decision(json.dumps({"action": "finish", "args": args}))

    def test_smoke_requires_observed_random_filename(self):
        class ObservationModel:
            calls = 0
            def decide(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps({"action": "list_files", "args": {"subdir": "."}})
                observation = json.loads(messages[-1]["content"].split("\n", 1)[1])
                return json.dumps({"action": "finish", "args": {
                    "answer": observation["data"]["files"][0]}})
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(run_smoke(ObservationModel(), Path(directory))["smoke_passed"])

    def test_smoke_rejects_premature_success(self):
        class PrematureModel:
            def decide(self, messages):
                return '{"action":"finish","args":{"answer":"success"}}'
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(run_smoke(PrematureModel(), Path(directory))["smoke_passed"])
