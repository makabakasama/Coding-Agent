"""Execution-only policy on the existing RepoTools, leaving experiment tools intact."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

from .task_contract import _relative_path
from .tools import RepoTools


CONFIG_NAMES = {"pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "conftest.py",
                ".coveragerc", "uv.lock", "poetry.lock", "pipfile", "pipfile.lock"}


def protected(path, targets):
    parts = path.lower().split("/")
    name = parts[-1]
    return (any(p in {"tests", "test", ".github", ".gitlab"} for p in parts)
            or name in CONFIG_NAMES or name.startswith(("test_", "requirements"))
            or name.endswith("_test.py")
            or any(path == t.split("::", 1)[0] or path.startswith(t.split("::", 1)[0] + "/") for t in targets))


class TaskRepoTools(RepoTools):
    def __init__(self, root, *, contract, **kwargs):
        super().__init__(root, allow_writes=contract["kind"] != "read_code", allow_tests=contract["allowTests"], **kwargs)
        self.contract = contract
        self.scopes = [(p, (self.root / p).is_dir()) for p in contract["allowedPaths"]]

    def _path(self, rel):
        if rel != "." and (not isinstance(rel, str) or not _relative_path(rel)):
            raise ValueError("Path is protected or invalid")
        path = super()._path(rel)
        if path != self.root / rel or path.name.lower() in {"id_rsa", "id_ed25519"}:
            raise ValueError("Linked or secret path is excluded")
        return path

    def _iter_files(self, directory, limit=1500):
        for path in super()._iter_files(directory, limit):
            try:
                self._path(path.relative_to(self.root).as_posix())
            except ValueError:
                continue
            yield path

    def editable(self, path):
        self._path(path)
        return (self.contract["kind"] != "read_code"
                and not protected(path, self.contract["checkTargets"])
                and any(path == scope or directory and path.startswith(scope + "/") for scope, directory in self.scopes))

    def apply_patch(self, patch, *, expected_file=None):
        if not isinstance(patch, str) or len(patch) > 60000:
            raise ValueError("Invalid patch")
        if re.search(r"^(?:rename |copy |old mode |new mode |new file mode |deleted file mode |GIT binary patch|Binary files|similarity index)", patch, re.M):
            raise PermissionError("Rename, copy, binary and file mode patches are unsupported")
        result = subprocess.run(["git", "-C", str(self.root), "apply", "--numstat", "-z", "-"],
                                input=patch.encode("utf-8"), capture_output=True, timeout=20)
        if result.returncode or not result.stdout:
            raise ValueError("Patch paths cannot be determined")
        paths = []
        for row in result.stdout.decode("utf-8").split("\0"):
            if not row:
                continue
            added, deleted, path = row.split("\t", 2)
            if not added.isdigit() or not deleted.isdigit() or not path or not self.editable(path):
                raise PermissionError("Patch exceeds allowed business-code scope")
            paths.append(path)
        # Both old and new headers must describe precisely the paths Git parsed.
        for header in re.findall(r"^(?:---|\+\+\+) (.+)$", patch, re.M):
            header = header.rstrip("\r")
            if header != "/dev/null" and header.removeprefix("a/").removeprefix("b/") not in paths:
                raise PermissionError("Unsupported patch header")
        return {**super().apply_patch(patch, expected_file=expected_file), "paths": paths}

    def run_tests(self, target="."):
        if getattr(self, "policy_check", lambda: True)() is not True:
            raise PermissionError("Workspace identity or scope audit failed")
        if target not in self.contract["checkTargets"]:
            raise PermissionError("Only frozen checkTargets may be run")
        result = super().run_tests(target)
        counts = None
        try:
            report = Path(result["test_log"]["directory"]) / "report.xml"
            raw = report.read_bytes()
            if self.test_output_filter is not None:
                filtered = self.test_output_filter(raw)
                if filtered != raw:
                    report.write_bytes(filtered)
                raw = filtered
            if len(raw) > 2 * 1024 * 1024 or b"<!DOCTYPE" in raw:
                raise ValueError("Invalid JUnit report")
            tree = ET.fromstring(raw)
            suites = [tree] if tree.tag == "testsuite" else list(tree.findall("testsuite"))
            if not suites:
                raise ValueError("Missing JUnit testsuites")
            values = {key: sum(int(s.attrib[key]) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
            passed = values["tests"] - values["failures"] - values["errors"] - values["skipped"]
            if min(*values.values(), passed) < 0:
                raise ValueError("Inconsistent JUnit counts")
            counts = {"total": values["tests"], "passed": passed, "failed": values["failures"],
                      "errors": values["errors"], "skipped": values["skipped"]}
        except (KeyError, OSError, ValueError, ET.ParseError):
            pass
        status, reason = "failed", None
        if result.get("error"):
            reason = "TEST_TIMEOUT" if result.get("timed_out") else "TEST_EXECUTION_OR_LOG_ERROR"
        elif counts is None:
            reason = "TEST_COUNTS_UNAVAILABLE"
        elif not counts["total"]:
            reason = "NO_TESTS_COLLECTED"
        elif result.get("exit_code") != 0 or counts["failed"] or counts["errors"]:
            reason = "TEST_FAILURE"
        elif counts["passed"] == 0:
            status, reason = "skipped", "ALL_TESTS_SKIPPED"
        else:
            status = "passed"
        result.update(counts=counts, check_status=status, check_reason=reason, passed=status == "passed")
        return result

    def execute(self, action, args):
        if action in ("apply_patch", "replace_text", "replace_lines", "run_tests"):
            try:
                if getattr(self, "policy_check", lambda: True)() is not True:
                    return {"ok": False, "error": "Workspace identity or scope audit failed; mutation/test blocked"}
            except Exception:
                return {"ok": False, "error": "Workspace audit unavailable; mutation/test blocked"}
        return super().execute(action, args)
