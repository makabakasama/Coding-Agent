"""Offline V0.5 discovery boundaries using temporary repositories and native decisions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.backend.services.task_execution_tools import TaskRepoTools
from repo_agent.core.agent import Agent
from repo_agent.core.context import latest_tool_result
from repo_agent.core.decisions import Decision, parse_tool_decision
from repo_agent.core.tools import RepoTools


class CodeDiscoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = RepoTools(self.root)

    def write(self, relative, content="value = 1\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def files(self, count, directory="src"):
        for number in range(count):
            self.write(f"{directory}/file_{number:04d}.py")

    def task_tools(self, **changes):
        contract = {"kind": "add_feature", "allowedPaths": ["editable.py"], "allowTests": False,
                    "checkTargets": [], "workspaceMode": "direct", "sourceKind": "directory", **changes}
        return TaskRepoTools(self.root, contract=contract, excluded_roots=[self.root / "runtime"])

    def test_empty_and_exact_page_lists_are_complete(self):
        empty = self.tools.list_files()
        self.assertEqual([], empty["files"])
        self.assertTrue(empty["complete"])
        self.assertFalse(empty["has_more"])
        self.assertIsNone(empty["next_cursor"])
        self.files(500)
        result = self.tools.list_files("src")
        self.assertEqual(500, result["returned"])
        self.assertTrue(result["complete"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def test_later_list_pages_do_not_drop_or_repeat_files(self):
        self.files(503)
        first = self.tools.list_files("src")
        self.assertEqual(500, len(first["files"]))
        self.assertFalse(first["complete"])
        self.assertTrue(first["has_more"])
        self.assertTrue(first["next_cursor"])
        second = self.tools.list_files("src", cursor=first["next_cursor"])
        paths = first["files"] + second["files"]
        self.assertEqual(503, len(paths))
        self.assertEqual(503, len(set(paths)))
        self.assertEqual(sorted(paths), paths)
        self.assertIn("src/file_0502.py", second["files"])
        self.assertTrue(second["complete"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])

    def test_small_list_limit_and_replayed_cursor_are_deterministic(self):
        self.files(5)
        first = self.tools.list_files("src", limit=2)
        cursor = first["next_cursor"]
        second = self.tools.list_files("src", cursor=cursor, limit=2)
        replay = self.tools.list_files("src", cursor=cursor, limit=2)
        self.assertEqual(second["files"], replay["files"])
        self.assertEqual(["src/file_0002.py", "src/file_0003.py"], second["files"])

    def test_search_continues_after_the_file_scan_budget(self):
        self.files(1501)
        self.write("src/file_1500.py", "needle = True\n")
        first = self.tools.search_code("needle", path="src")
        self.assertEqual([], first["matches"])
        self.assertEqual(1500, first["scanned_files"])
        self.assertFalse(first["scan_complete"])
        self.assertEqual("file_limit", first["stop_reason"])
        self.assertTrue(first["has_more"])
        second = self.tools.search_code("needle", path="src", cursor=first["next_cursor"])
        self.assertEqual(["src/file_1500.py"], [match["file"] for match in second["matches"]])
        self.assertEqual(1, second["matched_lines_total"])
        self.assertEqual(1, second["scanned_files"])
        self.assertTrue(second["scan_complete"])
        self.assertFalse(second["has_more"])

    def test_exact_match_limit_at_eof_is_complete(self):
        self.write("many.py", "needle\n" * 50)
        result = self.tools.search_code("needle", path="many.py")
        self.assertEqual(50, len(result["matches"]))
        self.assertEqual(50, result["matched_lines_total"])
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["has_more"])
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["next_cursor"])

    def test_single_file_matching_resumes_at_the_next_line(self):
        self.write("many.py", "needle\n" * 53)
        first = self.tools.search_code("needle", path="many.py")
        self.assertEqual(list(range(1, 51)), [match["line"] for match in first["matches"]])
        self.assertFalse(first["scan_complete"])
        self.assertEqual("match_limit", first["stop_reason"])
        second = self.tools.search_code("needle", path="many.py", cursor=first["next_cursor"])
        self.assertEqual([51, 52, 53], [match["line"] for match in second["matches"]])
        self.assertEqual(53, second["matched_lines_total"])
        self.assertTrue(second["scan_complete"])
        self.assertFalse(second["has_more"])

    def test_final_empty_page_preserves_prior_match_count(self):
        self.write("a.py", "needle\n" * 50)
        self.write("z.py", "no match\n")
        first = self.tools.search_code("needle")
        self.assertTrue(first["has_more"])
        final = self.tools.search_code("needle", cursor=first["next_cursor"])
        self.assertEqual([], final["matches"])
        self.assertEqual(50, final["matched_lines_total"])
        self.assertTrue(final["scan_complete"])

    def test_scoped_search_is_case_insensitive_and_accepts_directory_or_file(self):
        self.write("src/target.py", "Needle = 1\n")
        self.write("outside.py", "needle = 2\n")
        for scope in ("src", "src/target.py"):
            with self.subTest(scope=scope):
                result = self.tools.search_code("needle", path=scope)
                self.assertEqual(["src/target.py"], [match["file"] for match in result["matches"]])
                self.assertTrue(result["scan_complete"])
        absent = self.tools.search_code("unseen", path="src")
        self.assertEqual([], absent["matches"])
        self.assertEqual(0, absent["matched_lines_total"])
        self.assertTrue(absent["scan_complete"])

    def test_large_file_skip_remains_visible_on_later_pages(self):
        self.write("a_large.py", "x" * 700001)
        self.write("z_hits.py", "needle\n" * 51)
        first = self.tools.search_code("needle")
        self.assertEqual(1, first["skipped_counts"]["too_large"])
        final = self.tools.search_code("needle", cursor=first["next_cursor"])
        self.assertEqual(51, final["matched_lines_total"])
        self.assertEqual(1, final["skipped_counts"]["too_large"])
        self.assertFalse(final["scan_complete"])
        self.assertFalse(final["has_more"])
        self.assertIsNone(final["next_cursor"])

    def test_read_failure_cannot_be_reported_as_complete_no_match(self):
        blocked = self.write("blocked.py", "needle\n")
        real_open = Path.open

        def fail_read(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("synthetic unreadable source")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_read):
            result = self.tools.search_code("needle")
        self.assertEqual([], result["matches"])
        self.assertFalse(result["scan_complete"])
        self.assertEqual(1, result["skipped_counts"]["read_errors"])

    def test_traversal_failure_is_visible(self):
        self.write("src/hidden.py", "needle\n")

        def failed_walk(*args, **kwargs):
            error = PermissionError("synthetic inaccessible directory")
            callback = kwargs.get("onerror")
            if callback is None:
                raise error
            callback(error)
            yield from ()

        with patch("repo_agent.core.tools.os.walk", failed_walk):
            result = self.tools.execute("search_code", {"query": "needle"})
        if result["ok"]:
            self.assertFalse(result["data"]["scan_complete"])
            self.assertGreater(result["data"]["skipped_counts"]["enumeration_errors"], 0)
        else:
            self.assertIn("error", result)

    def test_list_traversal_failure_does_not_claim_complete(self):
        def failed_walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("synthetic inaccessible directory"))
            yield from ()

        with patch("repo_agent.core.tools.os.walk", failed_walk):
            result = self.tools.list_files()
        self.assertEqual([], result["files"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["has_more"])
        self.assertGreater(result["skipped_counts"]["enumeration_errors"], 0)

    def test_long_line_reports_text_truncation_separately(self):
        self.write("long.py", "needle " + "x" * 300 + "\n")
        result = self.tools.search_code("needle")
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["truncated"])
        self.assertEqual(240, len(result["matches"][0]["text"]))
        self.assertTrue(result["matches"][0]["text_truncated"])

    def test_output_limit_resumes_without_losing_matching_lines(self):
        relative = "a" * 60 + "/" + "b" * 60 + "/module_with_matching_lines.py"
        self.write(relative, ("needle " + '"' * 233 + "\n") * 60)
        matches = []
        result = self.tools.search_code("needle")
        self.assertEqual("output_limit", result["stop_reason"])
        for _ in range(10):
            matches.extend(result["matches"])
            if not result["has_more"]:
                break
            result = self.tools.search_code("needle", cursor=result["next_cursor"])
        self.assertFalse(result["has_more"])
        self.assertTrue(result["scan_complete"])
        self.assertEqual(list(range(1, 61)), [match["line"] for match in matches])
        self.assertEqual(60, result["matched_lines_total"])

    def test_cursor_is_bound_to_tool_query_scope_and_session(self):
        self.files(3)
        for number in range(3):
            self.write(f"src/file_{number:04d}.py", "needle\n")
        listed = self.tools.list_files("src", limit=1)
        searched = self.tools.search_code("needle", path="src", limit=1)
        cases = [("search_code", {"query": "needle", "path": "src", "cursor": listed["next_cursor"]}),
                 ("list_files", {"subdir": "src", "cursor": searched["next_cursor"]}),
                 ("search_code", {"query": "other", "path": "src", "cursor": searched["next_cursor"]}),
                 ("search_code", {"query": "needle", "path": ".", "cursor": searched["next_cursor"]}),
                 ("list_files", {"subdir": ".", "cursor": listed["next_cursor"]})]
        for action, args in cases:
            with self.subTest(action=action, args=args):
                self.assertFalse(self.tools.execute(action, args)["ok"])
        fresh = RepoTools(self.root)
        self.assertFalse(fresh.execute("list_files", {"subdir": "src", "cursor": listed["next_cursor"]})["ok"])

    def test_evicted_cursor_returns_a_recoverable_error(self):
        self.files(2)
        first = self.tools.list_files("src", limit=1)
        for _ in range(32):
            self.tools.list_files("src", limit=1)
        result = self.tools.execute("list_files", {"subdir": "src", "cursor": first["next_cursor"], "limit": 1})
        self.assertFalse(result["ok"], result)
        self.assertIn("cursor", result["error"])

    def test_invalid_limits_cursors_and_paths_are_rejected(self):
        self.write("visible.py")
        for action in ("list_files", "search_code"):
            for limit in (0, -1, True, "1", 501 if action == "list_files" else 51):
                args = {"limit": limit}
                if action == "search_code":
                    args["query"] = "needle"
                with self.subTest(action=action, limit=limit):
                    self.assertFalse(self.tools.execute(action, args)["ok"])
            for cursor in ("bogus", "x" * 65, 1):
                args = {"cursor": cursor}
                if action == "search_code":
                    args["query"] = "needle"
                with self.subTest(action=action, cursor=cursor):
                    self.assertFalse(self.tools.execute(action, args)["ok"])
        for scope in ("../escape", str(self.root), "missing", ".env", ".git"):
            with self.subTest(scope=scope):
                self.assertFalse(self.tools.execute("search_code", {"query": "needle", "path": scope})["ok"])

    def test_candidate_add_delete_and_rename_invalidate_list_cursor(self):
        self.files(3)
        for change in ("add", "delete", "rename"):
            with self.subTest(change=change):
                first = self.tools.list_files("src", limit=1)
                existing = self.root / "src/file_0002.py"
                added = self.root / "src/added.py"
                renamed = self.root / "src/renamed.py"
                if change == "add":
                    self.write("src/added.py")
                elif change == "delete":
                    existing.unlink()
                else:
                    existing.rename(renamed)
                result = self.tools.execute("list_files", {"subdir": "src", "cursor": first["next_cursor"], "limit": 1})
                self.assertFalse(result["ok"], result)
                if change == "add":
                    added.unlink()
                elif change == "delete":
                    self.write("src/file_0002.py")
                else:
                    renamed.rename(existing)

    def test_changed_resumed_file_rejects_search_cursor(self):
        path = self.write("many.py", "needle\n" * 51)
        first = self.tools.search_code("needle", path="many.py")
        original = path.read_bytes()
        path.write_bytes(original.replace(b"needle", b"NEEDLE", 1))
        result = self.tools.execute("search_code", {"query": "needle", "path": "many.py", "cursor": first["next_cursor"]})
        self.assertFalse(result["ok"], result)

    def test_previously_scanned_file_change_invalidates_cumulative_search(self):
        earlier = self.write("a.py", "unseen\n")
        self.write("z.py", "needle\n" * 51)
        first = self.tools.search_code("needle")
        previous_stat = earlier.stat()
        earlier.write_bytes(b"needle\n")
        os.utime(earlier, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        result = self.tools.execute("search_code", {"query": "needle", "cursor": first["next_cursor"]})
        self.assertFalse(result["ok"], result)

    def test_task_read_scope_is_distinct_from_edit_scope(self):
        self.write("editable.py")
        self.write("other/readable.py", "needle\n")
        self.write("runtime/private.py", "needle\n")
        self.write(".env.private.py", "needle\n")
        tools = self.task_tools()
        result = tools.search_code("needle")
        self.assertEqual(["other/readable.py"], [match["file"] for match in result["matches"]])
        self.assertTrue(result["scan_complete"])
        self.assertFalse(tools.editable("other/readable.py"))
        self.assertFalse(tools.execute("search_code", {"query": "needle", "path": "runtime"})["ok"])

    @unittest.skipUnless(os.name == "nt", "Windows junction traversal check")
    def test_junction_directory_is_pruned_before_external_descent(self):
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            (outside / "marker.py").write_bytes(b"outside_needle = True\n")
            linked = self.root / "linked"
            subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
                           capture_output=True, check=True)
            traversed = []
            real_scandir = os.scandir

            def scanned(path):
                traversed.append(Path(path))
                return real_scandir(path)

            try:
                with patch("repo_agent.core.tools.os.scandir", scanned):
                    listed = self.tools.list_files()
                    searched = self.tools.search_code("outside_needle")
                self.assertEqual([], listed["files"])
                self.assertEqual([], searched["matches"])
                self.assertNotIn(linked, traversed)
                self.assertNotIn(outside, traversed)
            finally:
                linked.rmdir()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO check")
    def test_fifo_with_text_suffix_is_excluded_without_opening_it(self):
        fifo = self.root / "pipe.py"
        os.mkfifo(fifo)
        real_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == fifo:
                raise AssertionError("Discovery must not open a FIFO")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            listed = self.tools.list_files()
            searched = self.tools.search_code("needle")
        self.assertEqual([], listed["files"])
        self.assertEqual([], searched["matches"])
        self.assertTrue(searched["scan_complete"])

    def test_single_file_task_only_lists_and_searches_the_selected_file(self):
        self.write("selected.py", "needle\n")
        self.write("other.py", "needle\n")
        tools = self.task_tools(targetFile="selected.py", allowedPaths=["selected.py"])
        self.assertEqual(["selected.py"], tools.list_files()["files"])
        self.assertEqual(["selected.py"], [match["file"] for match in tools.search_code("needle")["matches"]])
        self.assertFalse(tools.execute("search_code", {"query": "needle", "path": "other.py"})["ok"])

    def test_git_direct_ignores_untracked_ignored_files_and_keeps_tracked_source(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("tracked.py", "needle\n")
        self.write("visible.py", "needle\n")
        self.write("ignored/private.py", "needle\n")
        self.write(".gitignore", "ignored/\n")
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.py"], check=True)
        self.write(".gitignore", "ignored/\ntracked.py\n")
        tools = self.task_tools(sourceKind="git")
        self.assertEqual(["tracked.py", "visible.py"], tools.list_files()["files"])
        self.assertEqual(["tracked.py", "visible.py"], [match["file"] for match in tools.search_code("needle")["matches"]])
        selected = tools.search_code("needle", path="tracked.py")
        self.assertEqual(["tracked.py"], [match["file"] for match in selected["matches"]])
        self.assertFalse(tools.execute("search_code", {"query": "needle", "path": "ignored/private.py"})["ok"])
        self.assertFalse(tools.allow_tests)

    def test_worktree_mode_traversal_has_no_hidden_1500_file_limit(self):
        self.files(1501)
        self.write("src/file_1500.py", "needle\n")
        tools = self.task_tools(sourceKind="git", workspaceMode="worktree")
        first = tools.search_code("needle", path="src")
        self.assertFalse(first["scan_complete"])
        final = tools.search_code("needle", path="src", cursor=first["next_cursor"])
        self.assertEqual("src/file_1500.py", final["matches"][0]["file"])
        self.assertTrue(final["scan_complete"])

    def test_real_git_worktree_supports_list_and_scoped_search_pages(self):
        self.write("a.py", "needle = 1\n")
        self.write("src/b.py", "needle = 2\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "a.py", "src/b.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Offline", "-c",
                        "user.email=offline@example.invalid", "commit", "-qm", "discovery fixture"], check=True)
        workspace = self.root / "isolated-worktree"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "--detach", str(workspace), "HEAD"],
                       check=True, capture_output=True)
        contract = {"kind": "read_code", "allowedPaths": [], "allowTests": False, "checkTargets": [],
                    "workspaceMode": "worktree", "sourceKind": "git"}
        tools = TaskRepoTools(workspace, contract=contract)
        first = tools.list_files(limit=1)
        final = tools.list_files(limit=1, cursor=first["next_cursor"])
        self.assertEqual(["a.py", "src/b.py"], first["files"] + final["files"])
        self.assertTrue(final["complete"])
        result = tools.search_code("needle", path="src")
        self.assertEqual(["src/b.py"], [match["file"] for match in result["matches"]])
        self.assertTrue(result["scan_complete"])

    def test_native_agent_can_continue_listing_scope_search_and_read_target(self):
        self.files(501)
        self.write("src/file_0500.py", "needle = 'located'\n")
        tools = self.task_tools(kind="read_code", allowedPaths=[])

        class DiscoveryModel:
            def __init__(self):
                self.step = 0

            def decide(self, messages):
                self.step += 1
                observation = latest_tool_result(messages)["data"] if self.step > 1 else None
                if self.step == 1:
                    decision = Decision("list_files", {"subdir": "src"})
                elif self.step == 2:
                    decision = Decision("list_files", {"subdir": "src", "cursor": observation["next_cursor"]})
                elif self.step == 3:
                    decision = Decision("search_code", {"query": "needle", "path": observation["files"][0]})
                elif self.step == 4:
                    decision = Decision("read_file", {"path": observation["matches"][0]["file"]})
                else:
                    decision = Decision("finish", {"answer": "Located src/file_0500.py and read its needle assignment."})
                return parse_tool_decision(decision.assistant_message)

        with tempfile.TemporaryDirectory() as traces, patch("urllib.request.urlopen", side_effect=AssertionError("No model API")):
            agent = Agent(DiscoveryModel(), tools, trace_dir=Path(traces), max_steps=5)
            result = agent.run("Find and inspect the later-page needle assignment")
            events = [json.loads(line) for line in agent.trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("agent_reported_finished", result["status"], result)
        observations = [event["result"] for event in events if event["type"] == "observation"]
        self.assertTrue(all(observation["ok"] for observation in observations))
        self.assertFalse(observations[0]["data"]["complete"])
        self.assertTrue(observations[1]["data"]["complete"])
        self.assertTrue(observations[2]["data"]["scan_complete"])
        self.assertIn("located", observations[3]["data"]["content"])
