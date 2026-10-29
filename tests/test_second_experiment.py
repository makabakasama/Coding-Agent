"""Synthetic oracle/framework tests, never a reproduction of upstream #10525."""
import ast
import __future__
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from examples import browser_use_5998, pydantic_ai_10525 as entry
from repo_agent.decisions import Decision
from repo_agent.experiment_runner import ExperimentRunner
from repo_agent.experiments import ExperimentSpec, experiment_environment
from repo_agent.run_evidence import sha256


def regression_namespace(root):
    raw = (Path(entry.__file__).parent / "fixtures/pydantic_ai_10525/regression.patch").read_text(encoding="utf-8")
    added = "\n".join(line[1:] for line in raw.splitlines() if line.startswith("+") and not line.startswith("+++"))
    tree = ast.parse(added)
    selected = [node for node in tree.body if
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_ISSUE_10525_CHILD" for t in node.targets)
        or isinstance(node, ast.FunctionDef) and node.name == "_run_issue_10525_child"]
    def fail(message, **kwargs):
        raise RuntimeError(message)
    namespace = {"__file__": str(root / "tests/test_messages.py"), "sys": sys, "Path": Path,
                 "subprocess": subprocess, "tempfile": tempfile, "pytest": SimpleNamespace(fail=fail)}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "frozen-regression", "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class SyntheticOracleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "pydantic_ai_slim/pydantic_ai"
        self.package.mkdir(parents=True)
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        graph = self.root / "pydantic_graph/pydantic_graph"
        graph.mkdir(parents=True)
        (graph / "__init__.py").write_text("", encoding="utf-8")
        self.namespace = regression_namespace(self.root)

    def fixture(self, mode):
        (self.package / "messages.py").write_text('''import json, sys
MODE = ''' + repr(mode) + '''
INVALID_JSON_KEY = 'INVALID_JSON'
class ToolCallPart:
    def __init__(self, *, args=None, tool_name='example'):
        self.args = args
    def args_as_dict(self):
        if self.args is None or self.args == '':
            return {}
        if isinstance(self.args, dict):
            return self.args
        value = json.loads(self.args)
        if MODE == 'all-invalid' and sys.flags.optimize:
            return {INVALID_JSON_KEY: self.args}
        if isinstance(value, dict):
            return value
        if MODE == 'buggy' and sys.flags.optimize:
            return value
        return {INVALID_JSON_KEY: self.args}
class LoadCapabilityCallPart(ToolCallPart):
    @property
    def capability_id(self):
        return self.args_as_dict().get('id')
''', encoding="utf-8")

    def invoke(self, optimized):
        paths = os.pathsep.join((str(self.root / "pydantic_ai_slim"), str(self.root / "pydantic_graph")))
        with patch.dict(os.environ, {"PYTHONPATH": paths, "PYTHONOPTIMIZE": ""}), \
                patch.object(sys, "pycache_prefix", str(self.root / "cache")), redirect_stdout(io.StringIO()):
            self.namespace["_run_issue_10525_child"](optimized, SimpleNamespace(disabled=nullcontext))

    def test_normal_control_passes_but_buggy_optimized_behavior_has_only_target_marker(self):
        self.fixture("buggy")
        self.invoke(False)
        with self.assertRaises(RuntimeError) as error:
            self.invoke(True)
        self.assertIn(entry.experiment_spec().failure_contains, str(error.exception))
        self.assertNotIn("VALID_CONTROL_FAILURE", str(error.exception))

    def test_correct_synthetic_contract_passes_both_interpreters_without_child_asserts(self):
        self.fixture("correct")
        self.assertFalse(any(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(self.namespace["_ISSUE_10525_CHILD"]))))
        self.invoke(False)
        self.invoke(True)

    def test_broken_valid_inputs_are_rejected_with_a_different_marker(self):
        self.fixture("all-invalid")
        with self.assertRaises(RuntimeError) as error:
            self.invoke(True)
        self.assertIn("VALID_CONTROL_FAILURE", str(error.exception))
        self.assertNotIn(entry.experiment_spec().failure_contains, str(error.exception))

    def test_import_failure_and_wrong_optimization_do_not_identify_target_bug(self):
        (self.package / "messages.py").write_text("raise ImportError('synthetic missing dependency')\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as error:
            self.invoke(True)
        self.assertNotIn(entry.experiment_spec().failure_contains, str(error.exception))
        self.fixture("correct")
        paths = os.pathsep.join((str(self.root / "pydantic_ai_slim"), str(self.root / "pydantic_graph")))
        command = [sys.executable, "-O", "-X", "pycache_prefix=" + str(self.root / "cache"), "-c",
                   self.namespace["_ISSUE_10525_CHILD"], str(self.root), "0", str(self.root / "cache")]
        result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": paths})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Unexpected child optimization mode", result.stderr)
        self.assertNotIn(entry.experiment_spec().failure_contains, result.stdout)

    def test_spec_patch_checksum_and_separate_runner_configuration(self):
        spec = entry.experiment_spec()
        self.assertEqual(spec.regression_sha256, sha256(spec.regression_patch.read_bytes()))
        self.assertEqual(("pydantic_ai_slim/pydantic_ai/messages.py",), spec.editable_paths)
        self.assertIn("test_args_as_dict_raise_if_invalid_non_dict_json", "\n".join(spec.control_targets))
        old_task = browser_use_5998.TASK
        self.assertNotEqual(browser_use_5998._runner().task, entry._runner().task)
        self.assertEqual(old_task, browser_use_5998.TASK)
        self.assertNotEqual(browser_use_5998.experiment_spec().name, spec.name)
        env = experiment_environment(spec, self.root / "logs", self.root / "worktree")
        self.assertNotIn("{workspace}", env["PYTHONPATH"])
        self.assertNotIn(str(spec.source), env["PYTHONPATH"])


class WorkspaceEnvironmentTest(unittest.TestCase):
    def test_nested_source_imports_follow_each_fresh_stage_not_the_source_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "layout").mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(["git", "-c", "safe.directory=" + source.as_posix(),
                    "-C", str(source), *args], stderr=subprocess.PIPE)
            git("init", "-q")
            git("config", "core.autocrlf", "false")
            (source / "layout/pkg.py").write_bytes(b"def add(a, b):\n    return a - b\n")
            (source / "test_pkg.py").write_bytes(b"from pkg import add\n\ndef test_control():\n    assert add(2, 0) == 2\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "G")
            regression = root / "r.patch"
            regression.write_bytes(b"--- a/test_pkg.py\n+++ b/test_pkg.py\n@@ -2,3 +2,6 @@\n"
                b" \n def test_control():\n     assert add(2, 0) == 2\n+\n+def test_regression():\n+    assert add(2, 3) == 5\n")
            spec = ExperimentSpec(name="nested-source", source=source, revision=git("rev-parse", "HEAD").decode().strip(),
                test_python=Path(sys.executable), regression_target="test_pkg.py::test_regression",
                failure_contains="assert -1 == 5", control_targets=("test_pkg.py::test_control",),
                import_module="pkg", regression_patch=regression, regression_sha256=sha256(regression.read_bytes()),
                environment={"PYTHONPATH": "{workspace}/layout"}, editable_paths=("layout/pkg.py",))
            class Model:
                model, profile, base_url = "fixture", "fixture", "https://model.invalid/v1"
                calls = 0
                def decide(self, messages):
                    self.calls += 1
                    if self.calls == 1:
                        return Decision("read_file", {"path": "layout/pkg.py"})
                    if self.calls == 2:
                        observation = json.loads(messages[-1]["content"])
                        return Decision("replace_lines", {"snapshot_id": observation["data"]["snapshot_id"],
                            "start": 2, "end": 2, "new_text": "    return a + b\n"})
                    if self.calls == 3:
                        return Decision("run_tests", {"target": "test_pkg.py::test_regression"})
                    return Decision("finish", {"answer": "Synthetic candidate"})
            try:
                with patch("repo_agent.provider.urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
                    result = ExperimentRunner(root, "Synthetic nested import", 4, model_factory=Model).run_experiment(spec)
                self.assertEqual(0, result["exit_code"], result)
                before = json.loads(Path(result["prepare_manifest"]).read_text(encoding="utf-8"))
                verified = json.loads(Path(result["verification_report"]).read_text(encoding="utf-8"))
                fresh = json.loads(Path(verified["baseline_manifest"]).read_text(encoding="utf-8"))
                self.assertEqual(Path(before["workspace"]) / "layout", Path(before["test_environment"]["PYTHONPATH"]))
                self.assertEqual(Path(fresh["workspace"]) / "layout", Path(fresh["test_environment"]["PYTHONPATH"]))
                self.assertNotEqual(before["test_environment"], fresh["test_environment"])
                for manifest in (before, fresh):
                    self.assertTrue(Path(manifest["provenance"]["module_file"]).is_relative_to(Path(manifest["workspace"])))
                meta = json.loads(Path(result["test_logs"]["calls"][0]["metadata"]).read_text(encoding="utf-8"))
                self.assertEqual(before["test_environment"]["PYTHONPATH"], meta["test_environment"]["PYTHONPATH"])
                snapshot = json.loads(Path(result["agent_source"]["manifest"]).read_text(encoding="utf-8"))
                self.assertIn("examples/pydantic_ai_10525.py", snapshot["files"])
            finally:
                for workspace in (root / "experiments").glob("nested-source*"):
                    self.assertTrue(workspace.resolve().is_relative_to(root.resolve()))
                    git("worktree", "remove", "--force", str(workspace))


if __name__ == "__main__":
    unittest.main()
