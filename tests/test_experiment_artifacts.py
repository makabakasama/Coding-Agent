import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.experiments import _files, _sha
from repo_agent.experiment_artifacts import export_run_artifacts


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.original = b"def add(a, b):\n    return a - b\n"
        self.repaired = b"def add(a, b):\n    return a + b\n"
        (self.repo / "calc.py").write_bytes(self.original)
        (self.repo / "test_calc.py").write_bytes(b"# original tests\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "G")
        base = self.git("rev-parse", "HEAD").decode().strip()
        (self.repo / "test_calc.py").write_bytes(b"# original tests\n# frozen regression R\n")
        self.directory = self.root / "run"
        self.directory.mkdir()
        r = self.git("diff", "--binary")
        (self.directory / "regression.patch").write_bytes(r)
        (self.directory / "g-plus-r.patch").write_bytes(r)
        inventory = json.dumps(_files(self.repo)).encode("utf-8")
        (self.directory / "baseline-files.json").write_bytes(inventory)
        self.baseline = {
            "manifest": str(self.directory / "manifest.json"), "workspace": str(self.repo), "base_commit": base,
            "regression_patch_sha256": _sha(r), "g_plus_r_patch_sha256": _sha(r),
            "baseline_files_sha256": _sha(inventory),
        }
        manifest_bytes = json.dumps(self.baseline).encode("utf-8")
        (self.directory / "manifest.json").write_bytes(manifest_bytes)
        trace_dir = self.directory / "agent"
        trace_dir.mkdir()
        self.trace = trace_dir / "trace.jsonl"
        self.receipt = {"status": "step_limit", "editable_paths": ["calc.py"],
                        "manifest_sha256": _sha(manifest_bytes), "trace": str(self.trace)}
        self.write_trace([])

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, timeout=30).stdout

    def write_trace(self, observations):
        events = [{"type": "observation", "step": index, "action": "replace_lines", "result":
                   {"ok": True, "data": data}} for index, data in enumerate(observations, 1)]
        events.append({"type": "limit", "max_steps": 20})
        self.trace.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def repair(self):
        (self.repo / "calc.py").write_bytes(self.repaired)
        self.edit = {"path": "calc.py", "applied": True, "consistent": True,
                     "before_sha256": _sha(self.original), "after_sha256": _sha(self.repaired)}
        self.write_trace([self.edit])

    def export(self):
        return export_run_artifacts(self.baseline, self.receipt)

    def test_exports_only_p_and_replays_it_on_fresh_g_plus_r(self):
        self.repair()
        report = self.export()
        self.assertEqual("candidate_exported", report["status"], report)
        self.assertTrue(report["eligible_for_verification"])
        self.assertFalse(report["independently_verified"])
        candidate = Path(report["patch"]["path"]).read_bytes()
        self.assertNotIn(b"test_calc.py", candidate)
        self.assertEqual(_sha(candidate), report["patch"]["sha256"])
        fresh = (self.root / "fresh").resolve()
        self.assertTrue(fresh.is_relative_to(self.root.resolve()))
        self.git("worktree", "add", "--detach", str(fresh), self.baseline["base_commit"])
        try:
            for data in ((self.directory / "regression.patch").read_bytes(), candidate):
                subprocess.run(["git", "-C", str(fresh), "apply", "-"], input=data,
                               check=True, capture_output=True)
            self.assertEqual(self.repaired, (fresh / "calc.py").read_bytes())
            self.assertEqual((self.repo / "test_calc.py").read_bytes(), (fresh / "test_calc.py").read_bytes())
        finally:
            self.git("worktree", "remove", "--force", str(fresh))

    def test_no_code_change_does_not_export_r_as_p(self):
        report = self.export()
        self.assertEqual("no_code_change", report["status"], report)
        self.assertFalse(report["eligible_for_verification"])
        self.assertEqual(b"", Path(report["patch"]["path"]).read_bytes())

    def test_test_and_untracked_file_changes_are_preserved_but_rejected(self):
        self.repair()
        (self.repo / "test_calc.py").write_bytes(b"# weakened tests\n")
        (self.repo / "unexpected.py").write_bytes(b"unexpected = True\n")
        report = self.export()
        self.assertEqual("integrity_violation", report["status"], report)
        self.assertEqual(["test_calc.py", "unexpected.py"], report["integrity"]["protected_paths_changed"])
        self.assertTrue(Path(report["patch"]["path"]).is_file())
        self.assertEqual(b"# weakened tests\n", (self.repo / "test_calc.py").read_bytes())

    def test_restored_test_edits_remain_visible_in_the_trace(self):
        self.repair()
        self.write_trace([self.edit, {"path": "test_calc.py", "applied": True, "consistent": True}])
        report = self.export()
        self.assertEqual("integrity_violation", report["status"])
        self.assertEqual([], report["integrity"]["protected_paths_changed"])
        self.assertTrue(report["integrity"]["protected_edits_in_trace"])

    def test_source_drift_outside_recorded_edits_is_rejected(self):
        self.repair()
        (self.repo / "calc.py").write_bytes(self.repaired + b"# external change\n")
        report = self.export()
        self.assertEqual("integrity_violation", report["status"])
        self.assertFalse(report["integrity"]["trace_hashes_match_final_files"])

    def test_export_detects_changes_while_capturing_the_patch(self):
        self.repair()
        calls = 0
        def files(root):
            nonlocal calls
            calls += 1
            if calls == 2:
                (root / "calc.py").write_bytes(self.repaired + b"# concurrent edit\n")
            return _files(root)
        with patch("repo_agent.experiment_artifacts._files", side_effect=files):
            report = self.export()
        self.assertEqual("integrity_violation", report["status"])
        self.assertFalse(report["integrity"]["snapshot_consistent"])

    def test_broken_trace_retains_candidate_and_records_export_failure(self):
        self.repair()
        self.trace.write_text('{"partial":', encoding="utf-8")
        report = self.export()
        self.assertEqual("export_failed", report["status"])
        self.assertFalse(report["eligible_for_verification"])
        self.assertEqual("step_limit", report["agent_status"])
        self.assertTrue(Path(report["patch"]["path"]).is_file())
        self.assertTrue(Path(report["report"]).is_file())

    def test_staged_changes_cannot_hide_in_the_export(self):
        self.repair()
        self.git("add", "calc.py")
        report = self.export()
        self.assertEqual("integrity_violation", report["status"])
        self.assertFalse(report["integrity"]["index_empty"])
        self.assertGreater(report["patch"]["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
