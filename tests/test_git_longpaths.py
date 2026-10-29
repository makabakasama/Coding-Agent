"""Windows checkout regression using temporary repositories, never experiment inputs."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from repo_agent.experiments import ExperimentSpec, _files, _sha, prepare_experiment

from repo_agent.workspace import create_worktree


class GitLongPathsTest(unittest.TestCase):
    def test_default_and_explicit_override_are_per_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for override, expected in (({}, "true"), ({"core.longpaths": "false"}, "false")):
                with patch("repo_agent.workspace.subprocess.run", return_value=SimpleNamespace(stdout="commit")) as run:
                    create_worktree(root, root / "new", "HEAD", git_config=override)
                for call in run.call_args_list:
                    self.assertIn("core.longpaths=" + expected, call.args[0])
                    self.assertEqual(120, call.kwargs["timeout"])
                    self.assertTrue(call.kwargs["capture_output"])
                    self.assertTrue(call.kwargs["check"])

    @unittest.skipUnless(os.name == "nt", "Windows Git MAX_PATH regression")
    def test_windows_checkout_requires_per_command_longpaths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            source.mkdir()
            def git(*args):
                return subprocess.run(["git", "-c", "safe.directory=" + source.as_posix(),
                    "-c", "core.longpaths=true", "-C", str(source), *args], check=True,
                    capture_output=True, text=True, timeout=120).stdout.strip()
            git("init", "-q")
            relative = Path("a" * 65) / ("b" * 65) / ("c" * 35 + ".txt")
            original = source / relative
            original.parent.mkdir(parents=True)
            original.write_bytes(b"long-path fixture\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "G")
            destination = root / ("workspace-" + "w" * 70)
            self.assertGreaterEqual(len(str(destination / relative)), 260)
            config = {"safe.directory": source.as_posix(), "core.longpaths": "false"}
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                create_worktree(source, destination, "HEAD", git_config=config)
            self.assertIn("filename too long", failure.exception.stderr.lower())
            fixed = root / ("fixed-workspace-" + "w" * 70)
            try:
                # Production default, not an explicit test-only true override.
                result = create_worktree(source, fixed, "HEAD", git_config={"safe.directory": source.as_posix(), "core.autocrlf": "false"})
                self.assertEqual(str(fixed), result["workspace"])
                # Git supports extended paths even if this host's Python/OS
                # ordinary Win32 path handling still enforces MAX_PATH.
                extended = Path("\\\\?\\" + str(fixed / relative))
                self.assertEqual(original.read_bytes(), extended.read_bytes())
                inventory = _files(fixed)
                self.assertEqual({"sha256": _sha(original.read_bytes())}, inventory[relative.as_posix()])
                extended.write_bytes(b"changed long protected file\n")
                self.assertNotEqual(inventory, _files(fixed))
                extended.unlink()
                self.assertEqual({"missing": True}, _files(fixed)[relative.as_posix()])
            finally:
                if fixed.exists():
                    self.assertTrue(fixed.resolve().is_relative_to(root))
                    git("worktree", "remove", "--force", str(fixed))

    def test_inventory_preserves_relative_symlink_keys_and_uses_io_paths(self):
        name = "nested/" + "x" * 240 + "/link"
        roots = [Path.cwd()]
        if os.name == "nt":
            roots += [Path("\\\\?\\" + str(Path.cwd())), Path("\\\\server\\share")]
        for root in roots:
            with self.subTest(root=str(root)), \
                    patch("repo_agent.experiments._git", return_value=name.encode() + b"\0"), \
                    patch.object(Path, "is_symlink", return_value=True), \
                    patch.object(Path, "is_file", side_effect=AssertionError("symlink must not be opened")), \
                    patch("repo_agent.experiments.os.readlink", return_value="../target") as readlink:
                self.assertEqual({name: {"symlink": "../target"}}, _files(root))
                used = str(readlink.call_args.args[0])
                if os.name == "nt":
                    self.assertTrue(used.startswith("\\\\?\\"), used)
                    self.assertNotIn("\\\\?\\\\\\?\\", used)
                    if str(root).startswith("\\\\server"):
                        self.assertTrue(used.startswith("\\\\?\\UNC\\server\\share\\"), used)
                else:
                    self.assertEqual(str(root / name), used)


class GitFailureEvidenceTest(unittest.TestCase):
    def test_failed_checkout_saves_complete_streams_without_rewriting_prior_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            spec = ExperimentSpec(name="fixture", source=source, revision="HEAD", test_python=Path(sys.executable),
                regression_target="target", control_targets=("control",), failure_contains="target bug", import_module="calc")
            stderr = "diagnostic\n" * 1000 + "fatal: filename too long\n"
            failure = subprocess.CalledProcessError(128, ["git", "worktree", "add", "fixture"], output="full stdout\n", stderr=stderr)
            with patch("repo_agent.experiments.create_worktree", side_effect=failure) as create, \
                    patch("repo_agent.experiments._run_import_probe") as probe, \
                    patch("repo_agent.experiments._run_test") as tests:
                first = prepare_experiment(spec, workspace_parent=root / "workspaces", trace_parent=root / "runs")
                original = Path(first["manifest"]).read_bytes()
                second = prepare_experiment(spec, workspace_parent=root / "workspaces", trace_parent=root / "runs")
                self.assertEqual("true", create.call_args.kwargs["git_config"]["core.longpaths"])
                probe.assert_not_called()
                tests.assert_not_called()
            self.assertNotEqual(first["manifest"], second["manifest"])
            self.assertEqual(original, Path(first["manifest"]).read_bytes())
            self.assertEqual([], first["tests"])
            self.assertEqual("create_worktree", first["stage"])
            self.assertEqual("preparation_failed", first["status"])
            self.assertIn("filename too long", first["error"])
            evidence = first["git_failure"]
            self.assertEqual(128, evidence["exit_code"])
            self.assertFalse(evidence["timed_out"])
            self.assertEqual(failure.cmd, evidence["command"])
            run = Path(first["manifest"]).parent
            self.assertEqual(stderr.encode(), (run / evidence["stderr"]).read_bytes())
            self.assertEqual(b"full stdout\n", (run / evidence["stdout"]).read_bytes())

    def test_checkout_timeout_preserves_partial_bytes_and_does_not_start_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = ExperimentSpec(name="fixture", source=root, revision="HEAD", test_python=Path(sys.executable),
                regression_target="target", control_targets=("control",), failure_contains="bug", import_module="calc")
            failure = subprocess.TimeoutExpired(["git", "worktree", "add"], 120, output=b"partial\xff", stderr=b"timeout details")
            with patch("repo_agent.experiments.create_worktree", side_effect=failure), patch("repo_agent.experiments._run_test") as tests:
                result = prepare_experiment(spec, workspace_parent=root / "workspaces", trace_parent=root / "runs")
                tests.assert_not_called()
            evidence = result["git_failure"]
            self.assertTrue(evidence["timed_out"])
            self.assertIsNone(evidence["exit_code"])
            self.assertEqual(b"partial\xff", (Path(result["manifest"]).parent / evidence["stdout"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
