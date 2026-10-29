"""Temporary Git fixtures only: relocation must preserve frozen evidence checks."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.experiments import ExperimentSpec, _files, _sha, claim_baseline, validate_baseline
from repo_agent.experiment_artifacts import export_run_artifacts
from repo_agent.experiment_verify import _read_candidate
from repo_agent.path_migrations import recorded_path, recorded_environment


class PathMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.area = self.root / "experiments"
        self.source = self.area / "old-source"
        self.source.mkdir(parents=True)
        self.git(self.source, "init", "-q")
        self.git(self.source, "config", "core.autocrlf", "false")
        (self.source / "calc.py").write_bytes(b"value = 1\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "G")
        self.workspace = self.area / "old-workspace"
        self.git(self.source, "worktree", "add", "--detach", str(self.workspace), "HEAD")
        self.python = self.area / "old-env/Scripts/python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"fixture only; never executed")
        self.spec = ExperimentSpec(name="synthetic", source=self.source, revision="HEAD", test_python=self.python,
                                   regression_target="regression", control_targets=("control",), failure_contains="expected",
                                   import_module="calc", editable_paths=("calc.py",),
                                   environment={"PYTHONPATH": "{workspace}"})
        self.directory = self.root / "runs/fixture"
        self.directory.mkdir(parents=True)
        self.manifest = self.directory / "manifest.json"
        before = json.dumps(_files(self.workspace)).encode()
        (self.directory / "baseline-files.json").write_bytes(before)
        (self.directory / "g-plus-r.patch").write_bytes(b"")
        self.baseline = {"status": "baseline_verified", "stage": "complete", "baseline_files_unchanged": True,
            "manifest": str(self.manifest), "workspace": str(self.workspace), "source": str(self.source),
            "name": "synthetic", "test_python": str(self.python), "base_commit": self.git(self.source, "rev-parse", "HEAD").decode().strip(),
            "baseline_files_sha256": _sha(before), "g_plus_r_patch_sha256": _sha(b""),
            "regression_target": "regression", "control_targets": ["control"], "failure_contains": "expected",
            "import_module": "calc", "test_timeout": 90, "test_environment": {"PYTHONPATH": str(self.workspace)},
            "provenance": {"python": str(self.python), "module_file": str(self.workspace / "calc.py"), "packages": {}, "version": "fixture"},
            "tests": [{"target": name, "matches_expectation": True} for name in ("regression", "control")]}
        self.manifest.write_text(json.dumps(self.baseline), encoding="utf-8")
        self.map = self.area / "path-migrations.json"
        self.pairs = [(self.source, self.area / "project/inputs/source"),
                      (self.workspace, self.area / "project/workspaces/fixture"),
                      (self.python.parents[1], self.area / "project/inputs/env")]

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-c", "safe.directory=" + root.as_posix(), "-C", str(root), *args],
                              check=True, capture_output=True, timeout=30).stdout

    def relocate(self):
        for old, new in self.pairs:
            self.assertTrue(old.resolve().is_relative_to(self.root))
            self.assertTrue(new.resolve().is_relative_to(self.root))
            new.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.source, "worktree", "move", str(self.workspace), str(self.pairs[1][1]))
        self.source.rename(self.pairs[0][1])
        self.pairs[2][0].rename(self.pairs[2][1])
        self.git(self.pairs[0][1], "worktree", "repair", str(self.pairs[1][1]))
        self.map.write_text(json.dumps({"schema": 1, "status": "completed", "mappings":
                            [{"old": str(a), "new": str(b)} for a, b in self.pairs]}), encoding="utf-8")
        self.spec = replace(self.spec, source=self.pairs[0][1], test_python=self.pairs[2][1] / "Scripts/python.exe", path_migrations=self.map)

    def export(self):
        receipt = json.loads((self.directory / "agent-run.json").read_text())
        workspace = recorded_path(self.workspace, self.spec.path_migrations)
        repaired = b"value = 2\n"
        (workspace / "calc.py").write_bytes(repaired)
        trace = self.directory / "agent/trace.jsonl"
        trace.parent.mkdir()
        trace.write_text(json.dumps({"type": "observation", "step": 1, "action": "replace_lines", "result":
            {"ok": True, "data": {"path": "calc.py", "applied": True, "consistent": True,
             "before_sha256": _sha(b"value = 1\n"), "after_sha256": _sha(repaired)}}}) + "\n")
        receipt.update(status="step_limit", result={"status": "step_limit"}, trace=str(trace))
        if self.spec.path_migrations:
            receipt["path_migrations"] = str(self.map)
        report = export_run_artifacts(self.baseline, receipt)
        self.assertEqual("candidate_exported", report["status"], report)
        receipt["artifacts"] = {k: report[k] for k in ("status", "report", "eligible_for_verification", "independently_verified")}
        (self.directory / "agent-run.json").write_text(json.dumps(receipt))

    def read(self):
        destination = self.root / ("read-" + str(len(list(self.root.glob("read-*")))))
        destination.mkdir()
        return _read_candidate(self.manifest, self.spec, destination, {})

    def test_saved_candidate_survives_move_without_rewriting_and_rejects_tampering(self):
        claim_baseline(self.manifest, expected_spec=self.spec)
        self.export()
        frozen = {p.relative_to(self.directory).as_posix(): p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        self.relocate()
        self.assertEqual("step_limit", self.read()["agent_status"])
        self.assertEqual(frozen, {p.relative_to(self.directory).as_posix(): p.read_bytes() for p in self.directory.rglob("*") if p.is_file()})
        (self.directory / "artifacts/agent.patch").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.read()

    def test_unclaimed_baseline_can_be_checked_claimed_and_exported_after_move(self):
        frozen = self.manifest.read_bytes()
        self.relocate()
        self.assertEqual(self.baseline, validate_baseline(self.manifest, expected_spec=self.spec))
        claim_baseline(self.manifest, expected_spec=self.spec)
        self.export()
        self.assertEqual("step_limit", self.read()["agent_status"])
        self.assertEqual(frozen, self.manifest.read_bytes())
        with self.assertRaisesRegex(ValueError, "already has an Agent run"):
            validate_baseline(self.manifest, expected_spec=self.spec)

    def test_runs_directory_moves_preserve_bytes_and_old_manifest_entrypoint(self):
        claim_baseline(self.manifest, expected_spec=self.spec)
        self.export()
        frozen = {p.relative_to(self.directory).as_posix(): p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        self.relocate()
        destination = self.root / "runs/synthetic/run_20261029-144226_12345678/repair"
        self.assertTrue(destination.resolve().is_relative_to(self.root))
        self.assertTrue(self.directory.resolve().is_relative_to(self.root))
        destination.parent.mkdir(parents=True)
        self.directory.rename(destination)
        data = json.loads(self.map.read_text())
        data["mappings"].append({"old": str(self.directory), "new": str(destination)})
        self.map.write_text(json.dumps(data))
        self.assertEqual("step_limit", self.read()["agent_status"])
        self.assertEqual(frozen, {p.relative_to(destination).as_posix(): p.read_bytes() for p in destination.rglob("*") if p.is_file()})

    def test_mapping_requires_path_boundary_and_resolves_external_symlink(self):
        self.relocate()
        neighbour = self.area / "old-workspace-extra/file.py"
        self.assertEqual(neighbour, recorded_path(neighbour, self.map))
        self.assertEqual({"PYTHONPATH": str(self.pairs[1][1])}, recorded_environment({"PYTHONPATH": str(self.workspace)}, self.map))
        real_resolve = Path.resolve
        target = self.pairs[1][1] / "calc.py"
        outside = self.root / "external/calc.py"
        def resolve(path, *args, **kwargs):
            return outside if path == target else real_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.assertEqual(outside, recorded_path(self.workspace / "calc.py", self.map))
            self.assertFalse(recorded_path(self.workspace / "calc.py", self.map).is_relative_to(self.pairs[1][1]))
            with self.assertRaisesRegex(ValueError, "provenance"):
                validate_baseline(self.manifest, expected_spec=self.spec)

    def test_invalid_mapping_and_changed_checkout_are_rejected(self):
        self.relocate()
        (self.pairs[1][1] / "calc.py").write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "differs from the verified"):
            validate_baseline(self.manifest, expected_spec=self.spec)
        data = json.loads(self.map.read_text())
        data["mappings"][0]["new"] = str(self.root / "outside")
        self.map.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "inside"):
            recorded_path(self.source, self.map)
        data["mappings"][0]["new"] = str(self.pairs[0][1])
        data["mappings"].append({"old": str(self.workspace / "nested"), "new": str(self.area / "other")})
        self.map.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            recorded_path(self.workspace, self.map)
        data["status"] = "planned"
        self.map.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "not completed"):
            recorded_path(self.workspace, self.map)


if __name__ == "__main__":
    unittest.main()
