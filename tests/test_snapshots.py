import codecs
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.agent import Agent
from repo_agent.decisions import Decision
from repo_agent.tools import RepoTools


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "calc.py"
        self.original = b"def add(a, b):\n    return a - b\n"
        self.path.write_bytes(self.original)
        (self.root / "test_calc.py").write_bytes(b"from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.tools = RepoTools(self.root, allow_writes=True, allow_tests=True,
                               test_python=Path(sys.executable))

    def test_read_snapshot_hashes_the_same_full_bytes_as_display(self):
        result = self.tools.read_file("calc.py", start=2, end=2)
        self.assertEqual("2:     return a - b", result["content"])
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), result["sha256"])
        self.assertEqual((2, 2), (result["start"], result["end"]))
        self.assertIsInstance(result["snapshot_id"], str)

    def test_line_edit_handles_tabs_and_repeated_source_without_retyping_it(self):
        original = b"def f():\n\tif True:\n\t\treturn 1\n\treturn 1\n"
        self.path.write_bytes(original)
        read = self.tools.read_file("calc.py")
        result = self.tools.replace_lines(read["snapshot_id"], 3, 3, "\t\treturn 2")
        self.assertTrue(result["applied"], result)
        self.assertTrue(result["consistent"], result)
        self.assertEqual(b"def f():\n\tif True:\n\t\treturn 2\n\treturn 1\n", self.path.read_bytes())
        self.assertEqual(read["sha256"], result["before_sha256"])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), result["after_sha256"])

    def test_same_size_same_timestamp_change_outside_view_rejects_snapshot(self):
        read = self.tools.read_file("calc.py", start=2, end=2)
        previous_stat = self.path.stat()
        external = self.original.replace(b"def add", b"def sub")
        self.path.write_bytes(external)
        os.utime(self.path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        result = self.tools.execute("replace_lines", {
            "snapshot_id": read["snapshot_id"], "start": 2, "end": 2, "new_text": "    return a + b\n"})
        self.assertFalse(result["ok"], result)
        self.assertIn("whole-file version mismatch", result["error"])
        self.assertEqual(external, self.path.read_bytes())

    def test_success_expires_all_file_snapshots_even_after_content_is_restored(self):
        first = self.tools.read_file("calc.py")
        other = self.tools.read_file("calc.py", start=2, end=2)
        self.assertTrue(self.tools.replace_lines(first["snapshot_id"], 2, 2, "    return a + b\n")["applied"])
        fresh = self.tools.read_file("calc.py")
        self.assertTrue(self.tools.replace_lines(fresh["snapshot_id"], 2, 2, "    return a - b\n")["applied"])
        self.assertEqual(self.original, self.path.read_bytes())
        for old in (first, other, fresh):
            with self.subTest(snapshot=old["snapshot_id"]), self.assertRaisesRegex(ValueError, "expired"):
                self.tools.replace_lines(old["snapshot_id"], 2, 2, "    return 0\n")

    def test_unknown_snapshot_permissions_and_unread_ranges_leave_file_unchanged(self):
        read = self.tools.read_file("calc.py", start=1, end=1)
        for snapshot_id, start, end in (("unknown", 1, 1), (read["snapshot_id"], 2, 2),
                                        (read["snapshot_id"], True, 1)):
            with self.subTest(snapshot_id=snapshot_id, start=start), self.assertRaises(ValueError):
                self.tools.replace_lines(snapshot_id, start, end, "replacement\n")
        restricted = RepoTools(self.root)
        restricted_read = restricted.read_file("calc.py")
        with self.assertRaises(PermissionError):
            restricted.replace_lines(restricted_read["snapshot_id"], 1, 1, "replacement\n")
        self.assertEqual(self.original, self.path.read_bytes())

    def test_truncation_never_authorizes_unseen_or_partial_lines(self):
        self.path.write_bytes((b"# " + b"x" * 200 + b"\n") * 201)
        read = self.tools.read_file("calc.py", 1, 201)
        self.assertTrue(read["truncated"])
        self.assertLess(read["end"], 201)
        self.assertEqual(read["end"], len(read["content"].splitlines()))
        self.assertTrue(read["content"].endswith("x" * 200))
        with self.assertRaisesRegex(ValueError, "displayed lines"):
            self.tools.replace_lines(read["snapshot_id"], read["end"] + 1, read["end"] + 1, "changed\n")

    def test_line_edit_preserves_bom_newline_style_and_explicit_eof(self):
        for bom in (b"", codecs.BOM_UTF8):
            for newline in ("\n", "\r\n"):
                for trailing in ("", "\n"):
                    with self.subTest(bom=bool(bom), newline=repr(newline), trailing=bool(trailing)):
                        text = "# 中文" + newline + "\tvalue = 1" + (newline if trailing else "")
                        original = bom + text.encode("utf-8")
                        self.path.write_bytes(original)
                        read = self.tools.read_file("calc.py")
                        result = self.tools.replace_lines(read["snapshot_id"], 2, 2, "\tvalue = 2" + trailing)
                        self.assertTrue(result["applied"], result)
                        self.assertTrue(result["consistent"], result)
                        self.assertEqual(original.replace(b"value = 1", b"value = 2"), self.path.read_bytes())

    def test_delete_complete_lines_and_reject_noop(self):
        read = self.tools.read_file("calc.py")
        with self.assertRaisesRegex(ValueError, "no change"):
            self.tools.replace_lines(read["snapshot_id"], 2, 2, "    return a - b\n")
        self.assertTrue(self.tools.replace_lines(read["snapshot_id"], 2, 2, "")["applied"])
        self.assertEqual(b"def add(a, b):\n", self.path.read_bytes())

    def test_change_during_diff_generation_is_rejected_before_git(self):
        read = self.tools.read_file("calc.py")
        external = self.original + b"# external\n"
        real_diff = difflib.unified_diff

        def change_file(*args, **kwargs):
            yield from real_diff(*args, **kwargs)
            self.path.write_bytes(external)

        with patch("repo_agent.tools.difflib.unified_diff", side_effect=change_file), patch.object(
                self.tools, "apply_patch", wraps=self.tools.apply_patch) as apply:
            with self.assertRaisesRegex(ValueError, "changed while preparing"):
                self.tools.replace_lines(read["snapshot_id"], 2, 2, "    return a + b\n")
            apply.assert_not_called()
        self.assertEqual(external, self.path.read_bytes())

    def test_change_between_git_check_and_apply_is_rejected(self):
        read = self.tools.read_file("calc.py")
        external = self.original + b"# external\n"
        real_run = subprocess.run

        def change_file(command, **kwargs):
            result = real_run(command, **kwargs)
            if "--check" in command:
                self.path.write_bytes(external)
            return result

        with patch("repo_agent.tools.subprocess.run", side_effect=change_file) as run:
            result = self.tools.replace_lines(read["snapshot_id"], 2, 2, "    return a + b\n")
        self.assertFalse(result["applied"], result)
        self.assertEqual("version_check", result["stage"])
        self.assertEqual(1, run.call_count)
        self.assertEqual(external, self.path.read_bytes())

    def test_post_apply_conflict_is_reported_without_overwriting_external_edit(self):
        read = self.tools.read_file("calc.py")
        real_run = subprocess.run

        def change_file(command, **kwargs):
            result = real_run(command, **kwargs)
            if "--check" not in command and result.returncode == 0:
                self.path.write_bytes(self.path.read_bytes() + b"# external\n")
            return result

        with patch("repo_agent.tools.subprocess.run", side_effect=change_file):
            result = self.tools.replace_lines(read["snapshot_id"], 2, 2, "    return a + b\n")
        self.assertTrue(result["applied"], result)
        self.assertFalse(result["consistent"], result)
        self.assertIn("error", result)
        self.assertTrue(self.path.read_bytes().endswith(b"# external\n"))
        with self.assertRaisesRegex(ValueError, "expired"):
            self.tools.replace_lines(read["snapshot_id"], 2, 2, "    return 0\n")

    def test_agent_counts_snapshot_edit_and_invalidates_previous_test(self):
        class SnapshotModel:
            calls = 0

            def decide(self, messages):
                self.calls += 1
                if self.calls in (1, 4):
                    return Decision("read_file", {"path": "calc.py"})
                if self.calls in (2, 5):
                    read = json.loads(messages[-1]["content"])["data"]
                    return Decision("replace_lines", {"snapshot_id": read["snapshot_id"], "start": 2, "end": 2,
                                                       "new_text": "    return a + b\n" if self.calls == 2 else "    return a - b\n"})
                if self.calls == 3:
                    return Decision("run_tests", {"target": "test_calc.py"})
                return Decision("finish", {"answer": "Edited after testing; needs a rerun."})

        result = Agent(SnapshotModel(), self.tools, trace_dir=self.root / "trace", max_steps=6).run("Fix add")
        self.assertEqual("agent_reported_finished", result["status"], result)
        self.assertIsNone(result["last_test_passed"])
        self.assertFalse(result["verified_local_change"])
