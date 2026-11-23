"""Offline V0.5 tool boundaries and native multi-file delivery in synthetic repositories."""
import codecs
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.task_execution import file_patch
from repo_agent.backend.services.task_execution_tools import TaskRepoTools
from repo_agent.backend.services.task_service import TaskService
from repo_agent.core.agent import Agent
from repo_agent.core.context import latest_tool_result
from repo_agent.core.decisions import Decision, DecisionError, TOOL_DEFINITIONS, parse_tool_decision
from repo_agent.core.tools import RepoTools
from tests import test_tasks as fixtures
from tests.test_task_execution import ScriptedModel


class FileCreationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = RepoTools(self.root, allow_writes=True)

    def contract(self, **changes):
        return {"kind": "add_feature", "allowedPaths": ["."], "allowTests": False,
                "checkTargets": [], "sourceKind": "directory", "workspaceMode": "direct", **changes}

    def test_native_schema_and_argument_validation(self):
        schemas = {t["function"]["name"]: t["function"]["parameters"] for t in TOOL_DEFINITIONS}
        self.assertEqual(["path", "content"], schemas["create_file"]["required"])
        self.assertFalse(schemas["create_file"]["additionalProperties"])
        message = Decision("create_file", {"path": "new.py", "content": ""}).assistant_message
        self.assertEqual("create_file", parse_tool_decision(message).action)
        for args in ({"path": "new.py"}, {"path": "new.py", "content": 1},
                     {"path": "new.py", "content": "", "overwrite": True},
                     {"path": "", "content": ""}, {"path": "new.py", "content": "x" * 60001}):
            with self.subTest(args=str(args)[:100]), self.assertRaises(DecisionError):
                bad = deepcopy(message)
                bad["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
                parse_tool_decision(bad)

    def test_create_nested_utf8_text_and_empty_file_then_reread(self):
        content = '# 中文\r\nvalue = "ok"'
        result = self.tools.create_file("nested/module.py", content)
        self.assertTrue(result["applied"])
        self.assertTrue(result["consistent"])
        self.assertEqual(content.encode("utf-8"), (self.root / "nested/module.py").read_bytes())
        self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), result["sha256"])
        self.assertEqual(result["sha256"], result["after_sha256"])
        self.assertTrue(self.tools.read_file("nested/module.py")["snapshot_id"])
        self.assertTrue(self.tools.create_file("empty.py", "")["applied"])
        self.assertTrue(self.tools.read_file("empty.py")["snapshot_id"])
        self.assertFalse((self.root / ".git").exists())

    def test_existing_target_and_invalid_content_are_never_overwritten(self):
        path = self.root / "new.py"
        path.write_bytes(b"user content")
        for content in ("replacement", ""):
            self.assertFalse(self.tools.execute("create_file", {"path": "new.py", "content": content})["ok"])
            self.assertEqual(b"user content", path.read_bytes())
        for content in (None, "x" * 60001, "a\0b", "\ud800"):
            self.assertFalse(self.tools.execute("create_file", {"path": "other.py", "content": content})["ok"])
            self.assertFalse((self.root / "other.py").exists())

    def test_concurrent_creation_has_one_winner_and_no_overwrite(self):
        barrier = threading.Barrier(2)
        real_open = Path.open

        def synchronized_open(path, mode="r", *args, **kwargs):
            if mode == "xb":
                barrier.wait(timeout=10)
            return real_open(path, mode, *args, **kwargs)

        def create(content):
            return RepoTools(self.root, allow_writes=True).execute("create_file", {"path": "race.py", "content": content})

        with patch.object(Path, "open", synchronized_open), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["first\n", "second\n"]))
        self.assertEqual(1, sum(result["ok"] for result in results), results)
        winner = next(result["data"] for result in results if result["ok"])
        self.assertEqual(winner["sha256"], hashlib.sha256((self.root / "race.py").read_bytes()).hexdigest())

    def test_invalid_excluded_and_unsupported_paths_create_nothing(self):
        for path in ("../outside.py", str(self.root / "absolute.py"), ".git/x.py", ".GIT/x.py",
                     ".env", "nested/.ENV.local/x.py", "nested/id_rsa", "node_modules/x.py",
                     "a/../x.py", "a//x.py", "a/./x.py", "a\\x.py", "a.py:stream.py",
                     "NUL.py", "a./x.py", "a /x.py", "x.bin"):
            with self.subTest(path=path):
                self.assertFalse(self.tools.execute("create_file", {"path": path, "content": "value = 1"})["ok"])
        self.assertEqual([], list(self.root.iterdir()))

    def test_task_scope_protection_read_only_and_runtime_audit(self):
        for contract, path in ((self.contract(kind="read_code", allowedPaths=[]), "new.py"),
                               (self.contract(allowedPaths=["allowed.py"]), "other.py"),
                               (self.contract(targetFile="allowed.py"), "other.py"),
                               (self.contract(), "tests/new.py"), (self.contract(), "test_new.py"),
                               (self.contract(), "pyproject.toml"), (self.contract(), "conftest.py"),
                               (self.contract(), ".github/new.yml"),
                               (self.contract(checkTargets=["checks.py::test_fixed"]), "checks.py")):
            with self.subTest(path=path, contract=contract):
                tools = TaskRepoTools(self.root, contract=contract)
                self.assertFalse(tools.execute("create_file", {"path": path, "content": ""})["ok"])
        tools = TaskRepoTools(self.root, contract=self.contract(allowedPaths=["nested/new.py"]))
        tools.policy_check = lambda: False
        self.assertFalse(tools.execute("create_file", {"path": "nested/new.py", "content": ""})["ok"])
        self.assertFalse((self.root / "nested").exists())
        tools.policy_check = lambda: True
        self.assertTrue(tools.execute("create_file", {"path": "nested/new.py", "content": ""})["ok"])
        tools = TaskRepoTools(self.root, contract=self.contract(), excluded_roots=[self.root / "runtime"])
        self.assertFalse(tools.execute("create_file", {"path": "runtime/new.py", "content": ""})["ok"])
        self.assertFalse(RepoTools(self.root).execute("create_file", {"path": "readonly.py", "content": ""})["ok"])

    def test_ignored_git_path_is_rejected_in_direct_mode(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        tools = TaskRepoTools(self.root, contract=self.contract(sourceKind="git"))
        self.assertFalse(tools.execute("create_file", {"path": "ignored/new.py", "content": ""})["ok"])
        self.assertFalse((self.root / "ignored").exists())

    def test_reparse_attribute_is_rejected(self):
        directory = self.root / "linked"
        directory.mkdir()
        real_lstat = Path.lstat

        def lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if path == directory:
                class Reparse:
                    st_mode = result.st_mode
                    st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
                return Reparse()
            return result

        with patch.object(Path, "lstat", lstat):
            result = self.tools.execute("create_file", {"path": "linked/new.py", "content": ""})
        self.assertFalse(result["ok"], result)
        self.assertFalse((directory / "new.py").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction check")
    def test_real_junction_parent_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "linked"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)
        result = self.tools.execute("create_file", {"path": "linked/new.py", "content": ""})
        self.assertFalse(result["ok"], result)
        self.assertFalse((target / "new.py").exists())

    def test_empty_snapshot_write_preserves_bom_and_rejects_external_changes(self):
        for original in (b"", codecs.BOM_UTF8):
            with self.subTest(original=original):
                path = self.root / "empty.py"
                path.write_bytes(original)
                read = self.tools.read_file("empty.py")
                self.assertEqual((True, "", 0, 1, 1),
                                 (read["empty"], read["content"], read["total_lines"], read["start"], read["end"]))
                args = {"snapshot_id": read["snapshot_id"], "start": 1, "end": 1, "new_text": "value = 1\n"}
                result = self.tools.execute("replace_lines", args)
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["data"]["applied"], result)
                self.assertEqual(original + b"value = 1\n", path.read_bytes())
                self.assertFalse(self.tools.execute("replace_lines", args)["ok"])
                path.write_bytes(original)
                read = self.tools.read_file("empty.py")
                path.write_bytes(b"external = True\n")
                args["snapshot_id"] = read["snapshot_id"]
                self.assertFalse(self.tools.execute("replace_lines", args)["ok"])
                self.assertEqual(b"external = True\n", path.read_bytes())

    def test_create_counts_as_edit_and_invalidates_previous_test(self):
        tools = TaskRepoTools(self.root, contract=self.contract())
        # A pre-existing passing check is injected only to inspect Agent state invalidation.
        with patch.object(tools, "run_tests", return_value={"passed": True}):
            decisions = [Decision("run_tests", {}), Decision("create_file", {"path": "new.py", "content": ""}),
                         Decision("finish", {"answer": "Created an empty module"})]
            result = Agent(ScriptedModel(decisions), tools, trace_dir=self.root / "trace", max_steps=3).run("Create module")
        self.assertEqual("agent_reported_finished", result["status"])
        self.assertIsNone(result["last_test_passed"])
        self.assertFalse(result["verified_local_change"])

    def test_failed_create_does_not_satisfy_completion(self):
        (self.root / "existing.py").write_bytes(b"user content")
        tools = TaskRepoTools(self.root, contract=self.contract())
        decisions = [Decision("create_file", {"path": "existing.py", "content": ""}),
                     Decision("finish", {"answer": "Done"})]
        result = Agent(ScriptedModel(decisions), tools, trace_dir=self.root / "trace", max_steps=2).run("Create module")
        self.assertEqual("step_limit", result["status"])
        self.assertEqual(b"user content", (self.root / "existing.py").read_bytes())

    def test_empty_file_diff_can_be_applied_by_git(self):
        for name in ("new.py", "with space.py", "中文.py"):
            with self.subTest(name=name):
                raw = file_patch(name, None, "")
                result = subprocess.run(["git", "apply", "-"], cwd=self.root, input=raw, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(b"", (self.root / name).read_bytes())


class NativeMultiFileModel:
    def __init__(self):
        self.step = 0

    def decide(self, messages):
        self.step += 1
        if self.step == 1:
            decision = Decision("create_file", {"path": "modules/arithmetic.py", "content": "def add(a, b):\n    return a + b\n"})
        elif self.step == 2:
            decision = Decision("create_file", {"path": "modules/empty.py", "content": ""})
        elif self.step == 3:
            decision = Decision("read_file", {"path": "calc.py"})
        elif self.step == 4:
            read = latest_tool_result(messages)["data"]
            decision = Decision("replace_lines", {"snapshot_id": read["snapshot_id"], "start": 1, "end": 1,
                                                  "new_text": "from modules.arithmetic import add\nvalue = add(2, 3)\n"})
        elif self.step == 5:
            decision = Decision("run_tests", {"target": "tests/test_delivery.py"})
        else:
            decision = Decision("finish", {"answer": "Created arithmetic module and updated caller. Fixed check passed."})
        return parse_tool_decision(decision.assistant_message)


class FileDeliveryTest(unittest.TestCase):
    git = fixtures.TaskIntegrationTest.git

    def test_native_multi_file_delivery_diff_fixed_checks_and_direct_undo(self):
        for mode in ("worktree", "direct", "directory"):
            with self.subTest(mode=mode):
                fixtures.TaskIntegrationTest.setUp(self)
                source = self.repo
                if mode == "directory":
                    source = self.root / "ordinary"
                    source.mkdir()
                    (source / "calc.py").write_bytes(b"value = 1\n")
                (source / "tests").mkdir()
                (source / "tests/test_delivery.py").write_bytes(
                    b"from calc import value\n\ndef test_delivery():\n    assert value == 5\n")
                if mode != "directory":
                    self.git("add", ".")
                    self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "fixed check")
                original_caller = (source / "calc.py").read_bytes()
                service = TaskService(self.service.store, TaskProjects(self.config), workspace_root=self.root / "workspaces",
                                      model_factory=NativeMultiFileModel)
                record = service.create({"targetPath": str(source), "instruction": "Extract addition and update caller",
                                         "workspaceMode": "worktree" if mode == "worktree" else "direct",
                                         "allowTests": True, "checkTargets": ["tests/test_delivery.py"]})
                task_id = record["taskId"]
                if mode == "worktree":
                    service.prepare(task_id, {})
                with patch("urllib.request.urlopen", side_effect=AssertionError("No real provider allowed")):
                    state = service.execution.run_claimed(task_id, uuid.uuid4().hex)
                result = service.execution.result(task_id)
                self.assertEqual("agent_reported_finished", result["stopReason"], result)
                self.assertEqual("completed", result["completion"]["status"])
                self.assertEqual("passed", result["finalChecks"]["status"], result)
                self.assertEqual("passed", result["review"]["status"])
                self.assertEqual(["calc.py", "modules/arithmetic.py", "modules/empty.py"], result["review"]["changedPaths"])
                self.assertEqual("complete", result["delivery"]["status"])
                self.assertFalse(result["independentlyVerified"])
                directory = service.store.directory(task_id) / "execution"
                raw = (directory / "candidate.patch").read_bytes()
                for name in ("calc.py", "modules/arithmetic.py", "modules/empty.py"):
                    self.assertIn(name.encode(), raw)
                self.assertIn(b"+from modules.arithmetic import add", raw)
                events = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((directory / "events").glob("*.json"))]
                created = [e["feedback"] for e in events if e.get("action") == "create_file" and "feedback" in e]
                self.assertEqual(["modules/arithmetic.py", "modules/empty.py"], [e["path"] for e in created])
                self.assertTrue(all(e["applied"] for e in created))
                if mode == "directory":
                    self.assertFalse((source / ".git").exists())
                if mode == "worktree":
                    self.assertFalse((source / "modules").exists())
                else:
                    request = {"executionId": state["executionId"]}
                    module = source / "modules/arithmetic.py"
                    expected = module.read_bytes()
                    module.write_bytes(expected + b"# later user edit\n")
                    self.assertEqual(["modules/arithmetic.py"], service.undo.get(task_id)["conflicts"])
                    with self.assertRaises(TaskError):
                        service.undo.apply(task_id, request)
                    self.assertTrue(module.read_bytes().endswith(b"# later user edit\n"))
                    self.assertTrue((source / "modules/empty.py").exists())
                    module.write_bytes(expected)
                    self.assertEqual("undone", service.undo.apply(task_id, request)["status"])
                    self.assertFalse(module.exists())
                    self.assertFalse((source / "modules/empty.py").exists())
                    self.assertEqual(original_caller, (source / "calc.py").read_bytes())
                    self.assertEqual("agent_reported_finished", service.execution.result(task_id)["stopReason"])
