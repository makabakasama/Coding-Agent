import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.tools import RepoTools


class TestLogTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.tools = RepoTools(self.repo, allow_tests=True, test_python=Path(sys.executable),
                               test_log_dir=self.base / "test-logs", test_env={
                                   "ANONYMIZED_TELEMETRY": "false", "PRIVATE_API_KEY": "never-store-fixture-key"})

    def metadata(self, result):
        log = result["test_log"]
        path = Path(log["metadata"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), log["metadata_sha256"])
        data = json.loads(path.read_text(encoding="utf-8"))
        for name in ("stdout", "stderr"):
            raw = Path(data[name]["path"]).read_bytes()
            self.assertEqual(len(raw), data[name]["bytes"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), data[name]["sha256"])
        return data

    def test_full_separate_streams_are_saved_while_model_output_stays_bounded(self):
        stdout, stderr = b"begin-stdout\n" + b"A" * 9000, b"pytest warning\n" + b"B" * 7000
        with patch("repo_agent.tools.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, stderr)):
            first = self.tools.run_tests("test_fixture.py")
            second = self.tools.run_tests("test_fixture.py")
        self.assertTrue(first["passed"])
        self.assertEqual(6000, len(first["output"]))
        self.assertTrue(first["output_truncated"])
        meta = self.metadata(first)
        self.assertEqual(stdout, Path(meta["stdout"]["path"]).read_bytes())
        self.assertEqual(stderr, Path(meta["stderr"]["path"]).read_bytes())
        self.assertNotIn("--disable-warnings", meta["command"])
        self.assertEqual({"ANONYMIZED_TELEMETRY": "false"}, meta["test_environment"])
        self.assertNotIn("never-store-fixture-key", json.dumps(meta))
        self.assertNotIn("begin-stdout", first["output"])
        self.assertNotEqual(first["test_log"]["id"], second["test_log"]["id"])
        self.assertEqual([], list(self.repo.iterdir()))

    def test_failure_and_timeout_keep_output_and_actual_test_outcome(self):
        with patch("repo_agent.tools.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"failed", b"details")):
            result = self.tools.run_tests("test_fixture.py")
        self.assertFalse(result["passed"])
        self.assertEqual(1, self.metadata(result)["exit_code"])
        for output in (b"partial bytes\xff", "partial 中文"):
            timeout = subprocess.TimeoutExpired(["pytest"], 1, output=output, stderr=output)
            with self.subTest(output=type(output).__name__), patch("repo_agent.tools.subprocess.run", side_effect=timeout):
                result = self.tools.run_tests("test_fixture.py")
            self.assertFalse(result["passed"])
            self.assertTrue(result["timed_out"])
            self.assertIsNone(result["exit_code"])
            meta = self.metadata(result)
            expected = output.encode("utf-8") if isinstance(output, str) else output
            self.assertEqual(expected, Path(meta["stdout"]["path"]).read_bytes())
            self.assertEqual(expected, Path(meta["stderr"]["path"]).read_bytes())

    def test_log_initialization_failure_does_not_start_pytest(self):
        with patch.object(self.tools, "_save_test_metadata", side_effect=OSError("disk unavailable")), \
                patch("repo_agent.tools.subprocess.run") as run:
            result = self.tools.run_tests("test_fixture.py")
            run.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertEqual("write_failed", result["test_log"]["status"])

    def test_final_log_write_failure_cannot_report_a_passing_test(self):
        save = self.tools._save_test_metadata
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk unavailable")
            return save(*args)
        with patch.object(self.tools, "_save_test_metadata", side_effect=fail_second), \
                patch("repo_agent.tools.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"passed", b"")):
            result = self.tools.run_tests("test_fixture.py")
        self.assertEqual(0, result["exit_code"])
        self.assertFalse(result["passed"])
        self.assertTrue(result["logging_failed"])
        self.assertEqual("write_failed", self.tools.test_log_records[0]["status"])
        self.assertEqual(b"passed", (Path(result["test_log"]["directory"]) / "stdout.txt").read_bytes())

    def test_live_local_pytest_keeps_warnings_outside_worktree(self):
        file = self.repo / "test_fixture.py"
        file.write_text("import warnings\ndef test_warn():\n    warnings.warn('fixture-warning-evidence')\n", encoding="utf-8")
        before = file.read_bytes()
        with patch.dict(os.environ, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}):
            result = self.tools.run_tests("test_fixture.py")
        self.assertTrue(result["passed"], result)
        meta = self.metadata(result)
        self.assertIn("fixture-warning-evidence", Path(meta["stdout"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(before, file.read_bytes())
        self.assertFalse((self.repo / "test-logs").exists())

    def test_optional_logging_and_log_path_boundary(self):
        plain = RepoTools(self.repo, allow_tests=True)
        with patch("repo_agent.tools.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"ok", b"")):
            result = plain.run_tests("test_fixture.py")
        self.assertNotIn("test_log", result)
        self.assertFalse(result["output_truncated"])
        with self.assertRaisesRegex(ValueError, "outside"):
            RepoTools(self.repo, test_log_dir=self.repo / "logs")


if __name__ == "__main__":
    unittest.main()
