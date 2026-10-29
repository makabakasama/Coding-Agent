from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from examples import browser_use_5998 as entry
from repo_agent.agent import Agent
from repo_agent.decisions import Decision
from repo_agent.experiments import ExperimentSpec, _sha
from repo_agent.provider import ProviderError


class FixtureModel:
    model = "offline-fixture"
    profile = "fixture"
    base_url = "https://model.invalid/v1"

    def __init__(self, mode):
        self.mode, self.calls = mode, 0

    def decide(self, messages):
        self.calls += 1
        if self.mode == "no_change":
            return Decision("finish", {"answer": "No change; unverified."})
        if self.calls == 1:
            return Decision("read_file", {"path": "test_calc.py" if self.mode == "protected" else "calc.py"})
        if self.calls == 2:
            observation = json.loads(messages[-1]["content"])
            text = "    return a * b\n" if self.mode == "wrong" else "    return a + b\n"
            line = 2
            if self.mode == "protected":
                text, line = "# weakened import\n", 1
            return Decision("replace_lines", {"snapshot_id": observation["data"]["snapshot_id"],
                "start": line, "end": line, "new_text": text})
        if self.mode == "error":
            raise ProviderError("offline HTTP 429 fixture")
        if self.mode == "interrupted":
            raise KeyboardInterrupt
        if self.mode == "with_tests" and self.calls == 3:
            return Decision("run_tests", {"target": "test_calc.py::test_regression"})
        return Decision("finish", {"answer": "Candidate only; let independent tests decide."})


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.original = b"def add(a, b):\n    return a - b\n"
        (self.source / "calc.py").write_bytes(self.original)
        (self.source / "test_calc.py").write_bytes(
            b"from calc import add\n\ndef test_control():\n    assert add(2, 0) == 2\n")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "G")
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
        # A missing fixture patch must fail closed, never reach a real model endpoint.
        network = patch("repo_agent.provider.urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.providers = []

    def git(self, *args):
        return subprocess.run(["git", "-c", "safe.directory=" + self.source.as_posix(), "-C", str(self.source),
                               *args], check=True, capture_output=True, timeout=30).stdout

    def remove_worktrees(self):
        # These are only disposable synthetic fixtures, never real experiment paths.
        for workspace in (self.root / "experiments").glob("calculator*"):
            self.assertTrue(workspace.resolve().is_relative_to(self.root.resolve()))
            self.git("worktree", "remove", "--force", str(workspace))

    def invoke(self, mode="finish", *, spec=None, max_steps=3, agent_type=Agent):
        def create_provider():
            # The total report and preparation links must exist before model construction.
            current = [json.loads(path.read_text(encoding="utf-8")) for path in
                (self.root / "runs/calculator").glob("run_*/experiment.json")]
            current = [item for item in current if item["stage"] == "starting_agent"]
            self.assertEqual(1, len(current))
            self.assertEqual("baseline_verified", current[0]["prepare_status"])
            self.assertTrue(Path(current[0]["prepare_manifest"]).is_file())
            provider = FixtureModel(mode)
            self.providers.append(provider)
            return provider
        output = io.StringIO()
        with patch.object(entry, "PROJECT_ROOT", self.root), \
                patch.object(entry, "experiment_spec", return_value=spec or self.spec), \
                patch.object(entry, "OpenAICompatibleProvider", side_effect=create_provider), \
                patch.object(entry, "Agent", agent_type), patch.object(entry, "MAX_STEPS", max_steps), \
                redirect_stdout(output):
            code = entry.main(["--run"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, result["exit_code"])
        self.assertEqual(result, json.loads(Path(result["report"]).read_text(encoding="utf-8")))
        return result

    def test_one_command_prepares_repairs_exports_and_independently_verifies(self):
        result = self.invoke()
        round_root = Path(result["report"]).parent
        self.assertEqual("run_" + result["run_id"], round_root.name)
        baseline = json.loads(Path(result["prepare_manifest"]).read_text(encoding="utf-8"))
        verification = json.loads(Path(result["verification_report"]).read_text(encoding="utf-8"))
        self.assertEqual(result["run_id"], baseline["run_id"])
        self.assertEqual(result["run_id"], verification["run_id"])
        self.assertEqual(round_root / "repair/manifest.json", Path(result["prepare_manifest"]))
        self.assertEqual(round_root / "verify-01/verification.json", Path(result["verification_report"]))
        self.assertIn(result["run_id"] + "_agent", result["workspace"])
        self.assertIn(result["run_id"] + "_verify-01", result["verification_workspace"])
        self.assertIn("finish 不等于独立验收", (round_root / "summary.md").read_text(encoding="utf-8"))
        self.assertEqual((0, "agent_reported_finished", "independently_verified"),
            (result["exit_code"], result["agent_status"], result["verification_status"]))
        self.assertTrue(result["independently_verified"])
        self.assertEqual("candidate_exported", result["export_status"])
        for key in ("prepare_manifest", "agent_record", "candidate_report", "verification_report"):
            self.assertTrue(Path(result[key]).is_file())
        self.assertNotEqual(result["workspace"], result["verification_workspace"])
        self.assertEqual(self.original, (self.source / "calc.py").read_bytes())
        receipt = json.loads(Path(result["agent_record"]).read_text(encoding="utf-8"))
        self.assertFalse(receipt["artifacts"]["independently_verified"])
        verification = json.loads(Path(result["verification_report"]).read_text(encoding="utf-8"))
        self.assertEqual([1, 1], [test["counts"]["passed"] for test in verification["tests"]])

    def test_http_error_with_eligible_p_is_verified_and_agent_error_is_preserved(self):
        result = self.invoke("error")
        self.assertEqual((0, "error", "independently_verified"),
            (result["exit_code"], result["agent_status"], result["verification_status"]))
        self.assertEqual(1, result["agent_exit_code"])
        self.assertIn("429", result["agent_result"]["reason"])
        receipt = json.loads(Path(result["agent_record"]).read_text(encoding="utf-8"))
        self.assertEqual("error", receipt["status"])
        self.assertEqual("error", receipt["result"]["status"])

    def test_full_test_logs_and_real_agent_source_are_linked_in_receipt_and_total_report(self):
        result = self.invoke("with_tests", max_steps=4)
        self.assertEqual(0, result["exit_code"])
        self.assertEqual("recorded", result["evidence_status"])
        receipt = json.loads(Path(result["agent_record"]).read_text(encoding="utf-8"))
        for key in ("agent_source", "agent_source_check", "test_logs", "evidence_status"):
            self.assertEqual(receipt[key], result[key])
        snapshot = json.loads(Path(result["agent_source"]["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(Path(entry.__file__).resolve().parent.parent, Path(snapshot["source_root"]))
        self.assertNotEqual(self.root, Path(snapshot["source_root"]))
        self.assertIn("repo_agent/run_evidence.py", snapshot["files"])
        self.assertIn("examples/browser_use_5998.py", snapshot["files"])
        self.assertEqual("unchanged", result["agent_source_check"]["status"])
        self.assertTrue(Path(result["agent_source_check"]["report"]).is_file())
        calls = result["test_logs"]["calls"]
        self.assertEqual(1, len(calls))
        self.assertTrue(Path(calls[0]["metadata"]).is_file())
        self.assertFalse(Path(calls[0]["metadata"]).is_relative_to(Path(result["workspace"])))
        meta = json.loads(Path(calls[0]["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(0, meta["exit_code"])
        trace = Path(receipt["trace"]).read_text(encoding="utf-8").splitlines()
        observation = next(json.loads(line) for line in trace
            if json.loads(line).get("type") == "observation" and json.loads(line).get("action") == "run_tests")
        self.assertEqual(calls[0]["id"], observation["result"]["data"]["test_log"]["id"])
        self.assertLessEqual(len(observation["result"]["data"]["output"]), 6000)

    def test_initial_source_capture_failure_prevents_model_construction(self):
        with patch.object(entry, "archive_agent_source", side_effect=OSError("fixture archive failure")), \
                patch.object(entry, "verify_experiment") as verify:
            result = self.invoke()
            verify.assert_not_called()
        self.assertEqual([], self.providers)
        self.assertFalse(result["model_started"])
        self.assertEqual("startup_or_run_error", result["agent_status"])
        self.assertEqual("capture_failed", result["agent_source"]["status"])
        self.assertEqual("incomplete", result["evidence_status"])

    def test_step_limit_with_eligible_p_is_verified(self):
        result = self.invoke(max_steps=2)
        self.assertEqual((0, "step_limit", "independently_verified"),
            (result["exit_code"], result["agent_status"], result["verification_status"]))

    def test_outer_run_exception_after_edit_still_verifies_audited_candidate(self):
        class FailingAgent(Agent):
            def run(self, task):
                super().run(task)
                raise RuntimeError("fixture run failed after edits")
        result = self.invoke(agent_type=FailingAgent)
        self.assertEqual((0, "startup_or_run_error", "independently_verified"),
            (result["exit_code"], result["agent_status"], result["verification_status"]))
        receipt = json.loads(Path(result["agent_record"]).read_text(encoding="utf-8"))
        self.assertEqual("fixture run failed after edits", receipt["error"])

    def test_no_patch_and_integrity_violation_skip_with_saved_reasons(self):
        for mode, exported in (("no_change", "no_code_change"), ("protected", "integrity_violation")):
            with self.subTest(mode=mode), patch.object(entry, "verify_experiment") as verify:
                result = self.invoke(mode)
                self.assertEqual(1, result["exit_code"])
                self.assertEqual("skipped", result["verification_status"])
                self.assertIn(exported, result["verification_skip_reason"])
                self.assertEqual(exported, result["export_status"])
                verify.assert_not_called()

    def test_export_exception_skips_verification_without_losing_agent_result(self):
        with patch.object(entry, "export_run_artifacts", side_effect=RuntimeError("fixture export failure")), \
                patch.object(entry, "verify_experiment") as verify:
            result = self.invoke()
            verify.assert_not_called()
        self.assertEqual((1, "agent_reported_finished", "export_failed", "skipped"),
            (result["exit_code"], result["agent_status"], result["export_status"], result["verification_status"]))
        self.assertIn("export_failed", result["verification_skip_reason"])
        self.assertTrue(Path(result["agent_record"]).is_file())

    def test_prepare_failure_does_not_initialize_provider(self):
        with patch.object(entry, "verify_experiment") as verify:
            result = self.invoke(spec=replace(self.spec, failure_contains="missing target failure"))
            verify.assert_not_called()
        self.assertEqual([], self.providers)
        self.assertEqual((1, "preparation_failed", "not_started", "skipped"),
            (result["exit_code"], result["prepare_status"], result["agent_status"], result["verification_status"]))
        self.assertEqual("preparation_failed", result["verification_skip_reason"])
        self.assertTrue(Path(result["prepare_manifest"]).is_file())

    def test_wrong_p_independent_failure_is_never_reported_as_success(self):
        result = self.invoke("wrong")
        self.assertEqual((1, "agent_reported_finished", "verification_failed"),
            (result["exit_code"], result["agent_status"], result["verification_status"]))
        self.assertFalse(result["independently_verified"])
        self.assertEqual("patched_tests", result["verification_result"]["stage"])

    def test_user_interrupt_after_edit_skips_verification_even_with_eligible_p(self):
        with patch.object(entry, "verify_experiment") as verify:
            result = self.invoke("interrupted")
            verify.assert_not_called()
        self.assertEqual((130, "interrupted", "candidate_exported", "skipped"),
            (result["exit_code"], result["agent_status"], result["export_status"], result["verification_status"]))
        self.assertEqual("user_interrupted", result["verification_skip_reason"])

    def test_user_interrupt_in_prepare_preserves_report_and_exact_manifest_link(self):
        with patch("repo_agent.experiments._run_import_probe", side_effect=KeyboardInterrupt), \
                patch.object(entry, "verify_experiment") as verify:
            result = self.invoke()
            verify.assert_not_called()
        self.assertEqual(130, result["exit_code"])
        self.assertEqual("interrupted", result["prepare_status"])
        self.assertEqual([], self.providers)
        prepared = json.loads(Path(result["prepare_manifest"]).read_text(encoding="utf-8"))
        self.assertEqual("KeyboardInterrupt", prepared["error_type"])

    def test_user_interrupt_in_export_preserves_agent_status_and_skips_verify(self):
        with patch.object(entry, "export_run_artifacts", side_effect=KeyboardInterrupt), \
                patch.object(entry, "verify_experiment") as verify:
            result = self.invoke()
            verify.assert_not_called()
        self.assertEqual((130, "agent_reported_finished", "export_interrupted", "skipped"),
            (result["exit_code"], result["agent_status"], result["export_status"], result["verification_status"]))
        self.assertEqual("export", result["agent_result"]["interruption_stage"])

    def test_verify_internal_interrupt_is_reflected_without_repeating_it(self):
        real_verify = entry.verify_experiment
        def interrupt_verify(*args, **kwargs):
            with patch("repo_agent.experiments._run_import_probe", side_effect=KeyboardInterrupt):
                return real_verify(*args, **kwargs)
        with patch.object(entry, "verify_experiment", side_effect=interrupt_verify) as verify:
            result = self.invoke()
            self.assertEqual(1, verify.call_count)
        self.assertEqual((130, "agent_reported_finished", "interrupted"),
            (result["exit_code"], result["agent_status"], result["verification_status"]))
        self.assertEqual("verification_failed", result["verification_result"]["status"])
        self.assertTrue(result["verification_result"]["interrupted"])
        self.assertTrue(Path(result["verification_report"]).is_file())

    def test_eligibility_requires_actual_true_not_a_truthy_value(self):
        export = entry.export_run_artifacts
        def truthy(*args, **kwargs):
            report = export(*args, **kwargs)
            report["eligible_for_verification"] = 1
            return report
        with patch.object(entry, "export_run_artifacts", side_effect=truthy), \
                patch.object(entry, "verify_experiment") as verify:
            result = self.invoke()
            verify.assert_not_called()
        self.assertEqual(1, result["exit_code"])
        self.assertEqual("skipped", result["verification_status"])
        self.assertIn("eligible_for_verification_not_true", result["verification_skip_reason"])

    def test_repeat_run_is_fresh_and_standalone_verify_can_recheck_first_candidate(self):
        first = self.invoke()
        receipt = Path(first["agent_record"]).read_bytes()
        saved_report = Path(first["report"]).read_bytes()
        second = self.invoke()
        for key in ("report", "prepare_manifest", "workspace", "verification_workspace", "verification_report"):
            self.assertNotEqual(first[key], second[key])
        self.assertEqual(saved_report, Path(first["report"]).read_bytes())
        with patch.object(entry, "PROJECT_ROOT", self.root), \
                patch.object(entry, "experiment_spec", return_value=self.spec), \
                patch.object(entry, "OpenAICompatibleProvider") as provider, redirect_stdout(io.StringIO()):
            self.assertEqual(0, entry.main(["--verify", first["prepare_manifest"]]))
            provider.assert_not_called()
        self.assertEqual(receipt, Path(first["agent_record"]).read_bytes())
        self.assertEqual(saved_report, Path(first["report"]).read_bytes())
        self.assertTrue((Path(first["report"]).parent / "verify-02/verification.json").is_file())

    def test_run_is_mutually_exclusive_and_help_identifies_real_model_calls(self):
        with patch.object(entry, "OpenAICompatibleProvider") as provider, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entry.main(["--run", "--prepare"])
            self.assertEqual(2, raised.exception.code)
            provider.assert_not_called()
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            entry.main(["--help"])
        self.assertIn("REAL MODEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
