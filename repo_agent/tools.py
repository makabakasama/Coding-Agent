"""Restricted workspace tools. IMPORTANT: pytest still executes repository code!"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".mypy_cache",
             ".pytest_cache", "__pycache__", "dist", "build", ".tox", ".ruff_cache"}
SKIP_FILES = {".env", ".env.local", "id_rsa", "id_ed25519"}
TEXT_EXT = {".py", ".toml", ".md", ".txt", ".yaml", ".yml", ".json", ".ini", ".cfg", ".rst"}


class RepoTools:
    def __init__(self, root: Path, *, allow_writes: bool = False,
                 allow_tests: bool = False, test_timeout: int = 90,
                 test_python: Path | None = None):
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Repository root must be a directory")
        self.allow_writes = allow_writes
        self.allow_tests = allow_tests
        self.test_timeout = test_timeout
        self.test_python = str(test_python.resolve(strict=True)) if test_python else sys.executable
        if not Path(self.test_python).is_file():
            raise ValueError("Test Python must be an executable file")

    def _path(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
            raise ValueError("Path must be a nonempty relative path")
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Path escapes repository root")
        if any(part in SKIP_DIRS for part in Path(rel).parts) or candidate.name in SKIP_FILES:
            raise ValueError("Path is excluded")
        return candidate

    def _iter_files(self, directory: Path, limit: int = 1500):
        count = 0
        for base, dirs, names in os.walk(directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                             and not (Path(base) / d).is_symlink())
            for name in sorted(names):
                path = Path(base) / name
                if path.is_symlink() or name in SKIP_FILES or name.startswith('.env'):
                    continue
                if path.suffix.lower() not in TEXT_EXT:
                    continue
                yield path
                count += 1
                if count >= limit:
                    return

    def list_files(self, subdir: str = ".") -> dict:
        directory = self._path(subdir)
        if not directory.is_dir():
            raise ValueError("subdir must be a directory")
        paths = [p.relative_to(self.root).as_posix() for p in self._iter_files(directory, 500)]
        return {"files": paths, "returned": len(paths), "limit": 500}

    def search_code(self, query: str) -> dict:
        if not isinstance(query, str) or not (1 <= len(query) <= 100):
            raise ValueError("query length must be 1..100")
        hits = []
        for path in self._iter_files(self.root):
            try:
                if path.stat().st_size > 700_000:
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if query.casefold() in line.casefold():
                            hits.append({"file": path.relative_to(self.root).as_posix(),
                                         "line": line_num, "text": line.rstrip()[:240]})
                            if len(hits) >= 50:
                                return {"matches": hits, "truncated": True}
            except OSError:
                continue
        return {"matches": hits, "truncated": False}

    def read_file(self, path: str, start: int = 1, end: int = 160) -> dict:
        p = self._path(path)
        if not p.is_file() or p.stat().st_size > 800_000:
            raise ValueError("File missing or too large")
        if p.suffix.lower() not in TEXT_EXT:
            raise ValueError("Unsupported file type")
        start, end = int(start), int(end)
        if start < 1 or end < start or end - start > 200:
            raise ValueError("Invalid line range (max 201 lines)")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        content = "\n".join(f"{i}: {lines[i-1]}" for i in range(start, min(end, len(lines)) + 1))
        return {"file": path, "total_lines": len(lines), "content": content[:25000]}

    def apply_patch(self, patch: str) -> dict:
        if not self.allow_writes:
            raise PermissionError("Pass --allow-writes to enable patch modification")
        if not isinstance(patch, str) or not patch.strip() or len(patch) > 60_000:
            raise ValueError("Patch must be 1..60000 characters")
        headers = re.findall(r"^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\r\n ]+)", patch, re.M)
        if not headers or "@@" not in patch:
            raise ValueError("Patch must use unified diff file headers and hunks")
        for path in headers:
            if path != "/dev/null":
                self._path(path)
        # git apply rejects paths outside checkout without --unsafe-paths.
        for extra in ("--check", ""):
            args = ["git", "-C", str(self.root), "apply"]
            if extra:
                args.append(extra)
            args.extend(["--whitespace=nowarn", "-"])
            result = subprocess.run(args, input=patch, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    encoding="utf-8", errors="replace", timeout=20)
            if result.returncode != 0:
                return {"applied": False, "stage": extra or "apply",
                        "stderr": result.stderr[:3500]}
        return {"applied": True}

    def run_tests(self, target: str = ".") -> dict:
        if not self.allow_tests:
            raise PermissionError("Pass --allow-tests to enable running repo tests")
        if not isinstance(target, str) or not target or len(target) > 220:
            raise ValueError("Invalid test target")
        # no pytest option injection, no shell, no external path.
        base = target.split("::", 1)[0]
        if base.startswith("-") or any(c in target for c in ("\n", "\r", "\0")):
            raise ValueError("Invalid test target")
        self._path(base)
        command = [self.test_python, "-m", "pytest", "-q", "--disable-warnings", "--maxfail=1", target]
        try:
            # Equal-size edits within one timestamp tick can reuse stale .pyc files.
            # Each verification must import the current source, not prior bytecode.
            with tempfile.TemporaryDirectory(prefix="repo-agent-pycache-") as cache:
                command[1:1] = ["-X", "pycache_prefix=" + cache]
                result = subprocess.run(command, cwd=self.root, capture_output=True,
                                        text=True, encoding="utf-8", errors="replace",
                                        timeout=self.test_timeout)
            return {"passed": result.returncode == 0, "exit_code": result.returncode,
                    "output": (result.stdout + "\n" + result.stderr)[-6000:]}
        except subprocess.TimeoutExpired:
            return {"passed": False, "error": f"pytest timed out after {self.test_timeout}s"}

    def execute(self, action: str, args: dict) -> dict:
        handlers = {
            "list_files": self.list_files, "search_code": self.search_code,
            "read_file": self.read_file, "apply_patch": self.apply_patch,
            "run_tests": self.run_tests,
        }
        try:
            if action not in handlers or not isinstance(args, dict):
                raise ValueError(f"Unsupported action or args: {action}")
            return {"ok": True, "data": handlers[action](**args)}
        except (OSError, ValueError, TypeError, PermissionError,
                subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
