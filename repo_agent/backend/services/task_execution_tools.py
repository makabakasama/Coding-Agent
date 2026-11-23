"""Execution-only policy on the existing RepoTools, leaving experiment tools intact."""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

from repo_agent.backend.schemas.task_contract import _relative_path
from repo_agent.core.check_tools import CHECK_PREFIX, CheckToolRouting, run_independent_checks
from repo_agent.core.tools import RepoTools


CONFIG_NAMES = {"pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "conftest.py",
                ".coveragerc", "uv.lock", "poetry.lock", "pipfile", "pipfile.lock"}


def protected(path, targets):
    parts = path.lower().split("/")
    name = parts[-1]
    return (any(p in {"tests", "test", ".github", ".gitlab"} for p in parts)
            or name in CONFIG_NAMES or name.startswith(("test_", "requirements"))
            or name.endswith("_test.py")
            or any(path == t.split("::", 1)[0] or path.startswith(t.split("::", 1)[0] + "/") for t in targets))


class TaskRepoTools(CheckToolRouting, RepoTools):
    def __init__(self, root, *, contract, excluded_roots=(), **kwargs):
        self.excluded_roots = tuple(Path(path).resolve() for path in excluded_roots)
        super().__init__(root, allow_writes=contract["kind"] != "read_code", allow_tests=contract["allowTests"],
                         allow_ignored_test_logs=contract.get('workspaceMode') == 'direct', **kwargs)
        self.contract = contract
        self.checks = None
        self.scopes = [(p, (self.root / p).is_dir()) for p in contract["allowedPaths"]]

    def _path(self, rel):
        if rel != "." and (not isinstance(rel, str) or not _relative_path(rel)):
            raise ValueError("Path is protected or invalid")
        path = super()._path(rel)
        if any(path.is_relative_to(root) for root in self.excluded_roots):
            raise ValueError('Execution materials are outside the task source scope')
        if path != self.root / rel or path.name.lower() in {"id_rsa", "id_ed25519"}:
            raise ValueError("Linked or secret path is excluded")
        target = self.contract.get('targetFile')
        if target and rel != '.' and rel != target and not (path.is_dir() and target.startswith(rel.rstrip('/')+'/')):
            raise ValueError('Path is outside the selected file')
        return path

    def _iter_files(self, directory, limit=1500):
        if self.contract.get('targetFile'):
            path=self._path(self.contract['targetFile'])
            try:
                info = path.stat()
            except OSError:
                self._enumeration_errors += 1
                return
            if stat.S_ISREG(info.st_mode) and path.is_relative_to(directory):
                yield path
            return
        if self.contract.get('workspaceMode') == 'direct' and self.contract.get('sourceKind') != 'directory':
            from repo_agent.core.tools import TEXT_EXT
            listed = subprocess.run(['git', '-C', str(self.root), 'ls-files', '--cached', '--others',
                                     '--exclude-standard', '-z'], capture_output=True, check=True, timeout=30)
            count = 0
            for relative in sorted(set(listed.stdout.decode().split('\0')) - {''}):
                try:
                    path = self._path(relative)
                except ValueError:
                    continue
                if not path.is_relative_to(directory) or path.suffix.lower() not in TEXT_EXT:
                    continue
                try:
                    info = path.stat()
                except OSError:
                    self._enumeration_errors += 1
                    continue
                if stat.S_ISREG(info.st_mode):
                    yield path
                    count += 1
                    if limit is not None and count >= limit:
                        break
            return
        count = 0
        for path in super()._iter_files(directory, limit=None):
            try:
                self._path(path.relative_to(self.root).as_posix())
            except ValueError:
                continue
            yield path
            count += 1
            if limit is not None and count >= limit:
                break

    def _query_state(self, tool, scope, query, cursor):
        target = self._query_path(scope)
        if (target.is_file() and self.contract.get('workspaceMode') == 'direct'
                and self.contract.get('sourceKind') != 'directory'):
            listed = subprocess.run(['git', '-C', str(self.root), 'ls-files', '--cached', '--others',
                                     '--exclude-standard', '-z', '--', ':(literal)' + scope],
                                    capture_output=True, timeout=15)
            if listed.returncode != 0:
                raise ValueError('Cannot inspect Git query scope')
            if scope not in listed.stdout.decode('utf-8').split('\0'):
                raise ValueError('Query file is outside the visible Git scope')
        return super()._query_state(tool, scope, query, cursor)

    def _query_visibility(self):
        return {**super()._query_visibility(),
                'git_ignored_untracked_excluded': self.contract.get('workspaceMode') == 'direct'
                    and self.contract.get('sourceKind') != 'directory',
                'target_file': self.contract.get('targetFile')}

    def editable(self, path):
        self._path(path)
        if self.contract.get('workspaceMode') == 'direct' and self.contract.get('sourceKind') != 'directory':
            result = subprocess.run(['git', '-C', str(self.root), 'check-ignore', '--no-index', '-q', '--', path],
                                    capture_output=True, timeout=15)
            if result.returncode == 0:
                return False
            if result.returncode != 1:
                raise ValueError('Cannot inspect ignored paths')
        return (self.contract["kind"] != "read_code"
                and not protected(path, self.contract["checkTargets"])
                and any(scope == "." or path == scope or directory and path.startswith(scope + "/") for scope, directory in self.scopes))

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

    def create_file(self, path, content):
        if not self.editable(path):
            raise PermissionError("Creation exceeds allowed business-code scope")
        return super().create_file(path, content)

    def run_tests(self, target=".", review=None, independent_targets=None):
        if independent_targets:
            if self.checks is None:
                raise PermissionError("Independent checks require the developer-check runner")
            return run_independent_checks(self.run_tests, target, independent_targets, review)
        if getattr(self, "policy_check", lambda: True)() is not True:
            raise PermissionError("Workspace identity or scope audit failed")
        if not self.allow_tests:
            raise PermissionError("Tests are not authorized")
        if not isinstance(target, str) or not target or len(target) > 220:
            raise ValueError("Invalid test target")
        temporary = target.startswith(CHECK_PREFIX) and self.checks is not None and self.checks.files is not None
        if target not in self.contract["checkTargets"] and not temporary:
            raise PermissionError("Only frozen checkTargets may be run")
        if self.checks is not None:
            return self.checks.run(target, review=review)
        return self._run_authorized_check(target)

    def _run_authorized_check(self, target, *, scratch=None, changed_python=(),
                              source_version=None, environment_version=None):
        result = super().run_tests(target, scratch=scratch, changed_python=changed_python,
                                   source_version=source_version, environment_version=environment_version)
        if not result.get('check_reason') and not result['passed']:
            result['check_reason'] = result.get('failure_kind') or result.get('error')
        return result

    def execute(self, action, args):
        if action in ("apply_patch", "replace_text", "replace_lines", "create_file", "run_tests"):
            try:
                if getattr(self, "policy_check", lambda: True)() is not True:
                    return {"ok": False, "error": "Workspace identity or scope audit failed; mutation/test blocked"}
            except Exception:
                return {"ok": False, "error": "Workspace audit unavailable; mutation/test blocked"}
        return super().execute(action, args)
