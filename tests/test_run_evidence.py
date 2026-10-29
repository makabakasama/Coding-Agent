import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.run_evidence import archive_agent_source, check_agent_source, sha256, source_archive_summary


class SourceEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "source"
        (self.root / "repo_agent").mkdir(parents=True)
        (self.root / "examples").mkdir()
        (self.root / "repo_agent/agent.py").write_bytes(b"# committed agent\n")
        (self.root / "examples/browser_use_5998.py").write_bytes(b"# entry\n")
        self.destination = self.base / "evidence"

    def git(self, *args):
        return subprocess.run(["git", "-c", "safe.directory=" + self.root.as_posix(), "-C", str(self.root),
                               *args], check=True, capture_output=True).stdout

    def capture(self):
        return archive_agent_source(self.destination, source_root=self.root)

    def test_uncommitted_and_untracked_source_bytes_are_archived_with_git_status(self):
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base")
        expected_head = self.git("rev-parse", "HEAD").decode().strip()
        (self.root / "repo_agent/agent.py").write_bytes(b"# actual uncommitted agent\n")
        (self.root / "repo_agent/new_feature.py").write_bytes(b"# untracked implementation\n")
        report = self.capture()
        self.assertEqual("captured", report["status"])
        self.assertEqual(expected_head, report["git"]["head"])
        statuses = "\n".join(report["git"]["source_status_records"])
        self.assertIn(" M repo_agent/agent.py", statuses)
        self.assertIn("?? repo_agent/new_feature.py", statuses)
        for name, info in report["files"].items():
            data = (self.destination / "source" / name).read_bytes()
            self.assertEqual((self.root / name).read_bytes(), data)
            self.assertEqual(sha256(data), info["sha256"])

    def test_scope_excludes_env_nested_sensitive_directories_and_unrelated_files(self):
        for name in (".env", "repo_agent/.env", "repo_agent/.private.py", "repo_agent/nested/secrets.py",
                     "examples/other.py", "experiments/private.py", "runs/private.py", ".git/secret"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"never-copy-fixture-secret")
        report = self.capture()
        self.assertEqual({"repo_agent/agent.py", "examples/browser_use_5998.py"}, set(report["files"]))
        all_bytes = b"".join(path.read_bytes() for path in self.destination.rglob("*") if path.is_file())
        self.assertNotIn(b"never-copy-fixture-secret", all_bytes)
        self.assertFalse(report["loaded_code_equivalence"])
        self.assertEqual({"python", "python_version", "platform", "openssl"}, set(report["runtime"]))

    def test_non_git_source_still_captures_and_old_snapshot_is_never_overwritten(self):
        report = self.capture()
        self.assertEqual("captured", report["status"])
        self.assertFalse(report["git"]["available"])
        path = Path(report["manifest"])
        original = path.read_bytes()
        (self.root / "repo_agent/agent.py").write_bytes(b"# changed after snapshot\n")
        with self.assertRaises(FileExistsError):
            self.capture()
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(b"# committed agent\n", (self.destination / "source/repo_agent/agent.py").read_bytes())
        checked = check_agent_source(source_archive_summary(path))
        self.assertEqual("source_drift", checked["status"])
        self.assertEqual(["repo_agent/agent.py"], checked["changed_paths"])
        self.assertEqual(original, path.read_bytes())
        again = check_agent_source(source_archive_summary(path))
        self.assertNotEqual(checked["report"], again["report"])

    def test_second_explicit_entry_is_archived_without_including_other_examples(self):
        (self.root / "examples/pydantic_ai_10525.py").write_bytes(b"# second entry\n")
        (self.root / "examples/unrelated.py").write_bytes(b"never-copy-fixture-secret")
        report = self.capture()
        self.assertIn("examples/pydantic_ai_10525.py", report["files"])
        self.assertNotIn("examples/unrelated.py", report["files"])
        self.assertEqual("unchanged", check_agent_source(source_archive_summary(Path(report["manifest"])))["status"])

    def test_archive_tampering_is_detected_using_initial_hash(self):
        report = self.capture()
        path = Path(report["manifest"])
        summary = source_archive_summary(path)
        path.write_bytes(path.read_bytes() + b"\n")
        result = check_agent_source(summary)
        self.assertEqual("check_failed", result["status"])
        self.assertEqual("ValueError", result["error_type"])
        # Restore the original manifest, then detect changed archived bytes too.
        path.write_bytes(path.read_bytes()[:-1])
        (self.destination / "source/repo_agent/agent.py").write_bytes(b"# tampered archive\n")
        self.assertEqual("check_failed", check_agent_source(summary)["status"])

    def test_symlinked_source_is_refused_instead_of_copying_its_bytes(self):
        target = self.root / "repo_agent/agent.py"
        target.write_bytes(b"never-copy-linked-secret")
        with patch.object(Path, "is_symlink", new=lambda path: path == target):
            report = self.capture()
        self.assertEqual("capture_failed", report["status"])
        self.assertEqual({}, report["files"])
        self.assertNotIn(b"never-copy-linked-secret", Path(report["manifest"]).read_bytes())

    def test_source_changing_during_capture_is_not_called_stable(self):
        from repo_agent import run_evidence
        original = run_evidence._sources
        calls = 0
        def change(root):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / "repo_agent/agent.py").write_bytes(b"# concurrent edit\n")
            return original(root)
        with patch.object(run_evidence, "_sources", side_effect=change):
            report = self.capture()
        self.assertEqual("source_drift", report["status"])
        self.assertEqual(b"# committed agent\n", (self.destination / "source/repo_agent/agent.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
