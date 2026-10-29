import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.round_layout import allocate_round, allocate_verification, workspace_name, write_summary
from repo_agent.path_migrations import recorded_path
from repo_agent.experiments import ExperimentSpec
from repo_agent.experiment_verify import verify_experiment


class RoundLayoutTest(unittest.TestCase):
    def test_summary_distinguishes_agent_failure_and_does_not_rewrite_success(self):
        with tempfile.TemporaryDirectory() as temp:
            directory, _ = allocate_round(Path(temp), "run", "fixture")
            (directory / "repair").mkdir()
            (directory / "repair/manifest.json").write_text(json.dumps({"status": "baseline_verified", "stage": "complete"}))
            (directory / "repair/agent-run.json").write_text(json.dumps({"status": "error", "error": "rate limited"}))
            write_summary(directory)
            summary = (directory / "summary.md").read_text(encoding="utf-8")
            self.assertIn("`error`", summary)
            self.assertIn("rate limited", summary)
            original = b'{"status":"independently_verified","stage":"complete","independently_verified":true}'
            (directory / "experiment.json").write_bytes(original)
            verify, _ = allocate_verification(directory)
            (verify / "verification.json").write_text(json.dumps({"status": "verification_failed", "independently_verified": False, "error": "later replay failure"}))
            write_summary(directory)
            summary = (directory / "summary.md").read_text(encoding="utf-8")
            self.assertIn("`independently_verified`", summary)
            self.assertIn("later replay failure", summary)
            self.assertEqual(original, (directory / "experiment.json").read_bytes())

    def test_verification_rejects_wrong_area_or_round_metadata_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            spec = ExperimentSpec(name="fixture", source=root, revision="HEAD", test_python=root / "python",
                                  regression_target="r", control_targets=("c",), failure_contains="bug", import_module="calc")
            outside, _ = allocate_round(root / "other", "run", "fixture")
            (outside / "repair").mkdir()
            with self.assertRaisesRegex(ValueError, "outside"):
                verify_experiment(outside / "repair/manifest.json", expected_spec=spec,
                                  workspace_parent=root / "workspaces", trace_parent=root / "runs")
            self.assertEqual([], list(outside.glob("verify-*")))
            wrong, _ = allocate_round(root / "runs/fixture", "run", "another-experiment")
            (wrong / "repair").mkdir()
            with self.assertRaisesRegex(ValueError, "metadata"):
                verify_experiment(wrong / "repair/manifest.json", expected_spec=spec,
                                  workspace_parent=root / "workspaces", trace_parent=root / "runs")
            self.assertEqual([], list(wrong.glob("verify-*")))

    def test_allocation_retries_collision_and_verification_appends(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = "20261029-144226_12345678"
            second = "20261029-144226_abcdef12"
            with patch("repo_agent.round_layout.new_round_id", side_effect=[first, first, second]):
                one, identifier = allocate_round(root, "run", "pydantic-ai-10525")
                two, _ = allocate_round(root, "prepare", "pydantic-ai-10525")
            self.assertEqual(first, identifier)
            self.assertEqual("prepare_" + second, two.name)
            third = "20261029-144226_abcdef13"
            with patch("repo_agent.round_layout.new_round_id", side_effect=[first, second, third]):
                three, _ = allocate_round(root, "run", "pydantic-ai-10525")
            self.assertEqual("run_" + third, three.name)
            self.assertEqual("verify-01", allocate_verification(one)[1])
            self.assertEqual("verify-02", allocate_verification(one)[1])
            self.assertEqual("10525_" + first + "_agent", workspace_name("pydantic-ai-10525", identifier, "agent"))
            write_summary(one)
            text = (one / "summary.md").read_text(encoding="utf-8")
            self.assertIn("无测试结果", text)
            self.assertIn("未找到独立验收报告", text)
            self.assertTrue((root / "INDEX.md").is_file())

    def test_chain_spans_runs_and_experiments_but_rejects_cycle_and_area_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "experiments").mkdir()
            mapping = root / "experiments/path-migrations.json"
            pairs = [(root / "experiments/flat", root / "experiments/project/old"),
                     (root / "experiments/project/old", root / "experiments/project/new"),
                     (root / "runs/old", root / "runs/issue/run_round/repair")]
            data = {"schema": 1, "status": "completed", "mappings": [{"old": str(a), "new": str(b)} for a, b in pairs]}
            mapping.write_text(json.dumps(data))
            self.assertEqual(root / "experiments/project/new/file", recorded_path(root / "experiments/flat/file", mapping))
            self.assertEqual(root / "runs/issue/run_round/repair/manifest.json", recorded_path(root / "runs/old/manifest.json", mapping))
            self.assertEqual(root / "runs/old-neighbour/file", recorded_path(root / "runs/old-neighbour/file", mapping))
            data["mappings"].append({"old": str(root / "experiments/project/new"), "new": str(root / "experiments/flat")})
            mapping.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "Cyclic"):
                recorded_path(root / "runs/old/file", mapping)
            data["mappings"] = [{"old": str(root / "runs"), "new": str(root / "runs/sub") }]
            mapping.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "below their roots"):
                recorded_path(root / "runs/file", mapping)
