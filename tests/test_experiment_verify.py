from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from examples import browser_use_5998 as entry
from repo_agent.experiment_artifacts import export_run_artifacts
from repo_agent.experiment_verify import verify_experiment
from repo_agent.experiments import ExperimentSpec, _sha, claim_baseline, prepare_experiment


class VerifyExperimentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.original = (b"import os\n\ndef add(a, b):\n"
                         b"    if os.getenv('VERIFY_WRONG_FAILURE'):\n"
                         b"        raise RuntimeError('unrelated failure')\n"
                         b"    return a - b\n")
        (self.source / "calc.py").write_bytes(self.original)
        (self.source / "test_calc.py").write_bytes(
            b"from calc import add\n\ndef test_control():\n    assert add(2, 0) == 2\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "G")
        regression = self.root / "regression.patch"
        regression.write_bytes(b"--- a/test_calc.py\n+++ b/test_calc.py\n@@ -2,3 +2,6 @@\n"
            b" \n def test_control():\n     assert add(2, 0) == 2\n"
            b"+\n+def test_regression():\n+    assert add(2, 3) == 5\n")
        self.spec = ExperimentSpec(name="calculator", source=self.source,
            revision=self.git("rev-parse", "HEAD").decode().strip(), test_python=Path(sys.executable),
            regression_patch=regression, regression_sha256=_sha(regression.read_bytes()),
            regression_target="test_calc.py::test_regression", failure_contains="assert -1 == 5",
            control_targets=("test_calc.py::test_control",), import_module="calc", editable_paths=("calc.py",))
        self.addCleanup(self.remove_worktrees)
        self.baseline = prepare_experiment(self.spec, workspace_parent=self.root / "workspaces",
                                           trace_parent=self.root / "runs")
        self.assertEqual("baseline_verified", self.baseline["status"], self.baseline)
        self.manifest = Path(self.baseline["manifest"])
        claim_baseline(self.manifest, expected_spec=self.spec)

    def git(self, *args):
        return subprocess.run(["git", "-c", "safe.directory=" + self.source.as_posix(),
                               "-C", str(self.source), *args], check=True,
                              capture_output=True, timeout=30).stdout

    def remove_worktrees(self):
        # Only this synthetic test's disposable workspaces are removed.
        for workspace in (self.root / "workspaces").glob("calculator-*"):
            self.assertTrue(workspace.resolve().is_relative_to(self.root.resolve()))
            self.git("worktree", "remove", "--force", str(workspace))

    def export(self, replacement=b"a + b", *, protected=False):
        workspace = Path(self.baseline["workspace"])
        repaired = self.original.replace(b"a - b", replacement)
        (workspace / "calc.py").write_bytes(repaired)
        if protected:
            (workspace / "test_calc.py").write_bytes(b"# weakened test\n")
        trace = self.manifest.parent / "agent/trace.jsonl"
        trace.parent.mkdir()
        trace.write_text(json.dumps({"type": "observation", "step": 1, "action": "replace_lines",
            "result": {"ok": True, "data": {"path": "calc.py", "applied": True, "consistent": True,
                "before_sha256": _sha(self.original), "after_sha256": _sha(repaired)}}}) + "\n",
            encoding="utf-8")
        receipt_path = self.manifest.parent / "agent-run.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt.update(status="step_limit", model_started=True, result={"status": "step_limit"}, trace=str(trace))
        report = export_run_artifacts(self.baseline, receipt)
        receipt["artifacts"] = {k: report[k] for k in
            ("status", "report", "eligible_for_verification", "independently_verified")}
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return report

    def verify(self):
        result = verify_experiment(self.manifest, expected_spec=self.spec,
            workspace_parent=self.root / "workspaces", trace_parent=self.root / "runs")
        self.assertTrue(Path(result["report"]).is_file())
        return result

    def test_valid_saved_p_replays_on_fresh_g_plus_r_and_preserves_agent_status(self):
        exported = self.export()
        receipt = (self.manifest.parent / "agent-run.json").read_bytes()
        # The former workspace is deliberately unrelated; verification reads saved P only.
        (Path(self.baseline["workspace"]) / "calc.py").write_bytes(b"# later external edit\n")
        result = self.verify()
        self.assertEqual("independently_verified", result["status"], result)
        self.assertTrue(result["independently_verified"])
        self.assertFalse(result["model_started"])
        self.assertEqual("step_limit", result["agent_status"])
        self.assertEqual(exported["patch"]["sha256"], result["patch_sha256"])
        self.assertNotEqual(self.baseline["workspace"], result["workspace"])
        self.assertEqual([1, 0], [t["exit_code"] for t in result["baseline_tests"]])
        self.assertEqual([1, 1], [t["counts"]["passed"] for t in result["tests"]])
        self.assertEqual(receipt, (self.manifest.parent / "agent-run.json").read_bytes())
        self.assertEqual(self.original, (self.source / "calc.py").read_bytes())
        for test in result["tests"]:
            for key in ("stdout", "stderr", "junit"):
                self.assertTrue((Path(result["report"]).parent / test[key]).is_file())
        self.assertEqual(2, len(result["patch_commands"]))
        self.assertTrue(Path(result["provenance"]["module_file"]).is_relative_to(Path(result["workspace"])))

    def test_repeat_verify_never_reuses_or_overwrites_previous_attempt(self):
        self.export()
        first = self.verify()
        saved = Path(first["report"]).read_bytes()
        second = self.verify()
        self.assertTrue(first["independently_verified"], first)
        self.assertTrue(second["independently_verified"], second)
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.assertNotEqual(first["report"], second["report"])
        self.assertEqual(saved, Path(first["report"]).read_bytes())

    def test_wrong_p_keeps_failure_and_full_test_output(self):
        self.export(b"a * b")
        result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("patched_tests", result["stage"])
        self.assertFalse(result["independently_verified"])
        self.assertEqual(1, result["tests"][0]["counts"]["failed"])
        self.assertTrue(Path(result["workspace"]).is_dir())

    def test_fresh_baseline_must_fail_with_the_target_error(self):
        self.export()
        with patch.dict(os.environ, {"VERIFY_WRONG_FAILURE": "1"}):
            result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("reproduce_g_plus_r", result["stage"])
        self.assertEqual([], result["patch_commands"])
        self.assertEqual("baseline_tests", result["baseline_failure_stage"])

    def test_export_with_modified_protected_test_is_refused_before_replay(self):
        self.export(protected=True)
        result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("validate_materials", result["stage"])
        self.assertNotIn("workspace", result)

    def test_material_hash_changes_are_refused_and_each_attempt_is_retained(self):
        self.export()
        for name in ("manifest.json", "regression.patch", "baseline-files.json", "g-plus-r.patch",
                     "artifacts/agent.patch", "artifacts/final-files.json", "agent/trace.jsonl"):
            path = self.manifest.parent / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.subTest(name=name):
                result = self.verify()
                self.assertEqual("verification_failed", result["status"], result)
                self.assertFalse(result["independently_verified"])
                self.assertNotIn("workspace", result)
            path.write_bytes(original)

    def test_empty_p_is_refused_even_when_its_hash_matches(self):
        report = self.export()
        patch_path = Path(report["patch"]["path"])
        patch_path.write_bytes(b"")
        report["patch"].update(sha256=_sha(b""), bytes=0)
        Path(report["report"]).write_text(json.dumps(report), encoding="utf-8")
        result = self.verify()
        self.assertEqual("verification_failed", result["status"])
        self.assertIn("Empty candidate", result["error"])

    def test_p_cannot_smuggle_a_protected_test_change(self):
        report = self.export()
        patch_path = Path(report["patch"]["path"])
        data = patch_path.read_bytes() + (b"--- a/test_calc.py\n+++ b/test_calc.py\n@@ -1 +1 @@\n"
                                        b"-from calc import add\n+# weakened import\n")
        patch_path.write_bytes(data)
        report["patch"].update(sha256=_sha(data), bytes=len(data))
        Path(report["report"]).write_text(json.dumps(report), encoding="utf-8")
        result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("check_patch_paths", result["stage"])
        self.assertEqual([], result["patch_commands"])
        self.assertIn("unexpected path", result["error"])

    def test_passing_test_that_mutates_protected_files_cannot_pass_verification(self):
        self.export(b"(__import__('pathlib').Path('test_calc.py').write_text('# weakened') and (a + b))")
        result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("patched_tests", result["stage"])
        self.assertEqual(1, result["tests"][0]["counts"]["passed"])
        self.assertIn("protected files", result["error"])
        self.assertEqual(["test_calc.py"], result["integrity"]["protected_paths_changed"])
        self.assertTrue((Path(result["report"]).parent / "observed-files.json").is_file())

    def test_skipped_mandatory_regression_is_recorded_and_rejected(self):
        self.export(b"(__import__('pytest').skip('fixture skip') or (a + b))")
        result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("patched_tests", result["stage"])
        self.assertEqual(1, result["tests"][0]["counts"]["skipped"])
        self.assertIn("fixture skip", result["tests"][0]["skipped_cases"][0]["reason"])

    def test_consistently_hashed_wrong_final_inventory_cannot_pass_replay(self):
        report = self.export()
        path = Path(report["report"]).parent / "final-files.json"
        inventory = json.loads(path.read_text(encoding="utf-8"))
        inventory["calc.py"]["sha256"] = "0" * 64
        data = json.dumps(inventory).encode("utf-8")
        path.write_bytes(data)
        report["final_files_sha256"] = _sha(data)
        Path(report["report"]).write_text(json.dumps(report), encoding="utf-8")
        result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("check_applied_fingerprints", result["stage"])

    def test_changed_dependency_metadata_does_not_silently_pass_same_environment_gate(self):
        self.export()
        baseline = json.loads(self.manifest.read_text(encoding="utf-8"))
        baseline["provenance"]["packages"]["fixture-only-package-drift"] = "1.0"
        data = json.dumps(baseline).encode("utf-8")
        self.manifest.write_bytes(data)
        path = self.manifest.parent / "agent-run.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["manifest_sha256"] = _sha(data)
        path.write_text(json.dumps(receipt), encoding="utf-8")
        result = self.verify()
        self.assertEqual("verification_failed", result["status"], result)
        self.assertEqual("compare_environment", result["stage"])
        self.assertFalse(result["environment_comparison"]["package_versions_match"])
        self.assertEqual([], result["patch_commands"])

    def test_policy_change_is_refused_before_replay(self):
        self.export()
        with patch.object(entry, "experiment_spec", return_value=replace(self.spec, editable_paths=("test_calc.py",))), \
                patch.object(entry, "OpenAICompatibleProvider") as provider, \
                patch.object(entry, "PROJECT_ROOT", self.root), redirect_stdout(io.StringIO()):
            self.assertEqual(1, entry.main(["--verify", str(self.manifest)]))
            provider.assert_not_called()

    def test_verify_cli_never_initializes_provider_or_agent(self):
        self.export()
        with patch.object(entry, "experiment_spec", return_value=self.spec), \
                patch.object(entry, "PROJECT_ROOT", self.root), \
                patch.object(entry, "OpenAICompatibleProvider") as provider, \
                patch.object(entry, "Agent") as agent, redirect_stdout(io.StringIO()):
            self.assertEqual(0, entry.main(["--verify", str(self.manifest)]))
            provider.assert_not_called()
            agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
