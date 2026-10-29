from dataclasses import replace
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.experiments import ExperimentSpec, _capture, claim_baseline, prepare_experiment, validate_baseline
from examples import browser_use_5998 as entry


class PrepareExperimentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        (self.source / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
        (self.source / "test_calc.py").write_bytes(
            b"from calc import add\n\ndef test_control():\n    assert add(2, 0) == 2\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        self.patch = self.root / "regression.patch"
        self.patch.write_bytes(
            b"--- a/test_calc.py\n+++ b/test_calc.py\n@@ -2,3 +2,6 @@\n"
            b" \n def test_control():\n     assert add(2, 0) == 2\n"
            b"+\n+def test_regression():\n+    assert add(2, 3) == 5\n")
        self.spec = ExperimentSpec(
            name="calculator", source=self.source, revision=self.commit,
            test_python=Path(sys.executable), regression_patch=self.patch,
            regression_target="test_calc.py::test_regression", failure_contains="assert -1 == 5",
            control_targets=("test_calc.py::test_control",), import_module="calc",
            editable_paths=("calc.py",),
        )
        self.addCleanup(self.remove_worktrees)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.source), *args], check=True,
                              capture_output=True, timeout=30).stdout

    def remove_worktrees(self):
        # Only synthetic fixtures inside this test's TemporaryDirectory.
        for workspace in (self.root / "workspaces").glob("calculator*"):
            self.git("worktree", "remove", "--force", str(workspace))

    def prepare(self, spec=None):
        return prepare_experiment(spec or self.spec, workspace_parent=self.root / "workspaces",
                                  trace_parent=self.root / "runs")

    def test_repeat_runs_are_fresh_and_preserve_the_original_source(self):
        first = self.prepare()
        self.assertEqual("baseline_verified", first["status"], first)
        (Path(first["workspace"]) / "calc.py").write_text("previous agent edit", encoding="utf-8")
        second = self.prepare()
        self.assertEqual("baseline_verified", second["status"], second)
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.assertEqual("previous agent edit", (Path(first["workspace"]) / "calc.py").read_text())
        self.assertIn("return a - b", (self.source / "calc.py").read_text())
        directory = Path(second["manifest"]).parent
        self.assertEqual(self.patch.read_bytes(), (directory / "regression.patch").read_bytes())
        self.assertEqual(1, second["tests"][0]["counts"]["failed"])
        self.assertEqual(1, second["tests"][1]["counts"]["passed"])
        self.assertTrue((directory / "test_0.stdout.txt").is_file())
        self.assertFalse(second["model_started"])

    def test_unrelated_failure_is_recorded_and_never_ready(self):
        result = self.prepare(replace(self.spec, failure_contains="target TypeError absent from this assertion"))
        self.assertEqual("preparation_failed", result["status"])
        self.assertFalse(result["tests"][0]["matches_expectation"])
        self.assertTrue(Path(result["workspace"]).is_dir())
        saved = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual("preparation_failed", saved["status"])

    def test_importing_from_outside_worktree_is_rejected(self):
        result = self.prepare(replace(self.spec, import_module="json"))
        self.assertEqual("preparation_failed", result["status"])
        self.assertEqual("import_provenance", result["stage"])
        self.assertEqual([], result["tests"])

    def test_baseline_tests_cannot_mutate_the_prepared_source(self):
        # Add an otherwise passing control which changes a tracked file.
        (self.source / "test_mutation.py").write_bytes(
            b"def test_mutates():\n    from pathlib import Path\n"
            b"    Path('calc.py').write_text('changed by test')\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "mutating fixture")
        spec = replace(self.spec, revision=self.git("rev-parse", "HEAD").decode().strip(),
                       control_targets=("test_mutation.py::test_mutates",))
        result = self.prepare(spec)
        self.assertEqual("preparation_failed", result["status"], result)
        self.assertEqual("check_files", result["stage"], result)

    def test_skipped_control_is_not_counted_as_passing(self):
        (self.source / "test_skips.py").write_bytes(
            b"import pytest\n\ndef test_skips():\n    pytest.skip('not exercised')\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "skipped fixture")
        spec = replace(self.spec, revision=self.git("rev-parse", "HEAD").decode().strip(),
                       control_targets=("test_skips.py",))
        result = self.prepare(spec)
        self.assertEqual("preparation_failed", result["status"], result)
        self.assertEqual(1, result["tests"][-1]["counts"]["skipped"])
        self.assertFalse(result["tests"][-1]["matches_expectation"])

    def test_checksum_failure_keeps_an_audit_without_creating_a_worktree(self):
        result = self.prepare(replace(self.spec, regression_sha256="0" * 64))
        self.assertEqual("preparation_failed", result["status"])
        self.assertIn("checksum mismatch", result["error"])
        self.assertTrue(Path(result["manifest"]).is_file())
        self.assertFalse(Path(result["workspace"]).exists())

    def test_timeout_preserves_partial_tool_output(self):
        result = _capture([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(10)"],
                          self.source, dict(os.environ), self.root, "timeout", 1)
        self.assertTrue(result["timed_out"])
        self.assertIn(b"started", (self.root / result["stdout"]).read_bytes())

    def test_gate_detects_same_size_same_mtime_edits_and_new_or_missing_files(self):
        baseline = self.prepare()
        path = Path(baseline["manifest"])
        validate_baseline(path, expected_spec=self.spec)
        source = Path(baseline["workspace"]) / "calc.py"
        original, stamp = source.read_bytes(), source.stat()
        source.write_bytes(original.replace(b"a - b", b"a + b"))
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "calc.py"):
            validate_baseline(path, expected_spec=self.spec)
        source.write_bytes(original)
        extra = source.parent / "unexpected.py"
        extra.write_text("extra = True", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected.py"):
            validate_baseline(path, expected_spec=self.spec)
        extra.unlink()
        source.unlink()
        with self.assertRaisesRegex(ValueError, "calc.py"):
            validate_baseline(path, expected_spec=self.spec)
        source.write_bytes(original)
        validate_baseline(path, expected_spec=self.spec)

    def test_gate_rejects_changed_artifacts_failed_status_and_configuration(self):
        baseline = self.prepare()
        path = Path(baseline["manifest"])
        for filename in ("baseline-files.json", "regression.patch", "g-plus-r.patch"):
            artifact = path.parent / filename
            original = artifact.read_bytes()
            artifact.write_bytes(original + b"changed")
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "artifact changed"):
                validate_baseline(path, expected_spec=self.spec)
            artifact.write_bytes(original)
        original = path.read_bytes()
        baseline["status"] = "preparation_failed"
        path.write_text(json.dumps(baseline), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not verified"):
            validate_baseline(path, expected_spec=self.spec)
        path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "configuration differs"):
            validate_baseline(path, expected_spec=replace(self.spec, name="different-experiment"))
        with self.assertRaisesRegex(ValueError, "R overlaps"):
            validate_baseline(path, expected_spec=replace(self.spec, editable_paths=("test_calc.py",)))

    def test_gate_rejects_staging_and_commit_drift(self):
        baseline = self.prepare()
        path = Path(baseline["manifest"])
        workspace = Path(baseline["workspace"])
        def git(*args):
            subprocess.run(["git", "-C", str(workspace), *args], check=True, capture_output=True)
        git("add", "test_calc.py")
        with self.assertRaisesRegex(ValueError, "staged changes"):
            validate_baseline(path, expected_spec=self.spec)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "changed baseline")
        with self.assertRaisesRegex(ValueError, "HEAD differs"):
            validate_baseline(path, expected_spec=self.spec)

    def test_claim_is_single_use_even_without_source_edits(self):
        baseline = self.prepare()
        path = Path(baseline["manifest"])
        original_manifest = path.read_bytes()
        claim_baseline(path, expected_spec=self.spec)
        with self.assertRaisesRegex(ValueError, "already has an Agent run"):
            claim_baseline(path, expected_spec=self.spec)
        self.assertEqual(original_manifest, path.read_bytes())
        receipt = json.loads((path.parent / "agent-run.json").read_text(encoding="utf-8"))
        self.assertEqual("claimed", receipt["status"])
        self.assertFalse(receipt["model_started"])

    def test_cli_check_and_drift_refusal_never_initialize_a_provider(self):
        baseline = self.prepare()
        path = Path(baseline["manifest"])
        with patch.object(entry, "experiment_spec", return_value=self.spec), \
                patch.object(entry, "OpenAICompatibleProvider") as provider, redirect_stdout(io.StringIO()):
            self.assertEqual(0, entry.main(["--check-manifest", str(path)]))
            self.assertFalse((path.parent / "agent-run.json").exists())
            (Path(baseline["workspace"]) / "calc.py").write_text("changed", encoding="utf-8")
            self.assertEqual(1, entry.main(["--manifest", str(path)]))
            self.assertFalse((path.parent / "agent-run.json").exists())
            provider.assert_not_called()

    def test_cli_records_a_mock_run_and_cannot_reuse_its_baseline(self):
        spec = replace(self.spec, environment={"PREPARE_TEST_MARKER": "recorded"})
        baseline = self.prepare(spec)
        path = Path(baseline["manifest"])
        test_case = self
        class StubAgent:
            def __init__(self, model, tools, *, trace_dir, max_steps):
                self.trace_path = trace_dir / "stub.jsonl"
                self.tools = tools
                trace_dir.mkdir()
                test_case.assertEqual(baseline["test_environment"], tools.test_env)
                test_case.assertEqual(str(spec.test_python.resolve()), tools.test_python)
            def run(self, task):
                snapshot = self.tools.read_file("calc.py")
                edit = self.tools.replace_lines(snapshot["snapshot_id"], 2, 2, "    return a + b\n")
                events = [{"type": "observation", "step": 1, "action": "replace_lines",
                           "result": {"ok": True, "data": edit}}, {"type": "limit", "max_steps": 20}]
                self.trace_path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
                return {"status": "step_limit", "steps": 20, "last_test_passed": None,
                        "trace": str(self.trace_path)}
        with patch.object(entry, "experiment_spec", return_value=spec), \
                patch.object(entry, "OpenAICompatibleProvider") as provider, \
                patch.object(entry, "Agent", StubAgent), redirect_stdout(io.StringIO()):
            provider.return_value.model = "fixture-model"
            provider.return_value.profile = "fixture"
            provider.return_value.base_url = "https://username:fake-password@model.invalid/v1?api_key=fake-secret"
            self.assertEqual(0, entry.main(["--manifest", str(path)]))
            self.assertEqual(1, entry.main(["--manifest", str(path)]))
            self.assertEqual(1, provider.call_count)
        receipt_text = (path.parent / "agent-run.json").read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        self.assertEqual("step_limit", receipt["status"])
        self.assertEqual("model.invalid", receipt["model"]["api_host"])
        self.assertNotIn("fake-password", receipt_text)
        self.assertNotIn("fake-secret", receipt_text)
        self.assertEqual(path.parent / "agent", Path(receipt["trace"]).parent)
        self.assertEqual("candidate_exported", receipt["artifacts"]["status"])
        self.assertFalse(receipt["artifacts"]["independently_verified"])
        self.assertTrue(Path(receipt["agent_source"]["manifest"]).is_file())
        self.assertEqual("captured", receipt["agent_source"]["status"])
        self.assertTrue(Path(receipt["test_logs"]["directory"]).is_dir())

    def test_cli_exception_keeps_original_error_and_exports_partial_edit(self):
        baseline = self.prepare()
        path = Path(baseline["manifest"])
        class FailingAgent:
            def __init__(self, model, tools, *, trace_dir, max_steps):
                self.tools = tools
                trace_dir.mkdir()
                self.trace_path = trace_dir / "partial.jsonl"
            def run(self, task):
                snapshot = self.tools.read_file("calc.py")
                edit = self.tools.replace_lines(snapshot["snapshot_id"], 2, 2, "    return a + b\n")
                self.trace_path.write_text(json.dumps({"type": "observation", "step": 1,
                    "action": "replace_lines", "result": {"ok": True, "data": edit}}) + "\n", encoding="utf-8")
                raise RuntimeError("fixture crash after edit")
        with patch.object(entry, "experiment_spec", return_value=self.spec), \
                patch.object(entry, "OpenAICompatibleProvider") as provider, \
                patch.object(entry, "Agent", FailingAgent), redirect_stdout(io.StringIO()):
            provider.return_value.model = "fixture-model"
            provider.return_value.profile = "fixture"
            provider.return_value.base_url = "https://model.invalid/v1"
            self.assertEqual(1, entry.main(["--manifest", str(path)]))
        receipt = json.loads((path.parent / "agent-run.json").read_text(encoding="utf-8"))
        self.assertEqual("startup_or_run_error", receipt["status"])
        self.assertEqual("startup_or_run_error", receipt["result"]["status"])
        self.assertEqual("fixture crash after edit", receipt["error"])
        self.assertEqual("candidate_exported", receipt["artifacts"]["status"])
        report = json.loads(Path(receipt["artifacts"]["report"]).read_text(encoding="utf-8"))
        self.assertEqual("startup_or_run_error", report["agent_status"])
        candidate = Path(report["patch"]["path"]).read_bytes()
        self.assertIn(b"+    return a + b", candidate)
        self.assertNotIn(b"test_calc.py", candidate)

    def test_old_repo_option_is_rejected_before_model_initialization(self):
        with patch.object(entry, "OpenAICompatibleProvider") as provider, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                entry.main(["--repo", str(self.source)])
            self.assertEqual(2, exc.exception.code)
            provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
