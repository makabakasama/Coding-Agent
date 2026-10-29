"""Restricted workspace tools. IMPORTANT: pytest still executes repository code!"""
from __future__ import annotations
import codecs
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from .run_evidence import sha256

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".mypy_cache",
             ".pytest_cache", "__pycache__", "dist", "build", ".tox", ".ruff_cache"}
SKIP_FILES = {".env", ".env.local", "id_rsa", "id_ed25519"}
TEXT_EXT = {".py", ".toml", ".md", ".txt", ".yaml", ".yml", ".json", ".ini", ".cfg", ".rst"}


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    data: bytes
    sha256: str
    start: int
    end: int


class RepoTools:
    """
    包含
    _iter_files: 遍历文件
    list_files: 列出文件
    search_code: 搜索代码
    read_file: 读取文件
    replace_lines: 替换行
    replace_text: 替换文本
    apply_patch: 应用补丁
    run_tests: 运行测试
    等功能
    """
    def __init__(self, root: Path, *, allow_writes: bool = False,
                 allow_tests: bool = False, test_timeout: int = 90,
                 test_python: Path | None = None, test_env: dict[str, str] | None = None,
                 test_log_dir: Path | None = None):
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Repository root must be a directory")
        self.allow_writes = allow_writes
        self.allow_tests = allow_tests
        self.test_timeout = test_timeout
        self.test_env = dict(test_env or {})
        self.test_log_dir = test_log_dir.resolve() if test_log_dir is not None else None
        if self.test_log_dir is not None:
            if self.test_log_dir == self.root or self.test_log_dir.is_relative_to(self.root):
                raise ValueError("Test logs must be outside the tested worktree")
            self.test_log_dir.mkdir(parents=True, exist_ok=True)
        self.test_log_records: list[dict] = []
        self._snapshots: dict[str, FileSnapshot] = {}
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
        data = p.read_bytes()
        if len(data) > 800_000:
            raise ValueError("File too large")
        # Display and version must come from the same single byte read.
        lines = data.decode("utf-8-sig").splitlines()
        visible = []
        size = 0
        for i in range(start, min(end, len(lines)) + 1):
            line = f"{i}: {lines[i-1]}"
            extra = len(line) + bool(visible)
            if size + extra > 25000:
                if not visible:
                    raise ValueError("Line too long to display completely")
                break
            visible.append(line)
            size += extra
        visible_end = start + len(visible) - 1
        digest = hashlib.sha256(data).hexdigest()
        snapshot_id = None
        if visible:
            snapshot_id = "snapshot_" + uuid.uuid4().hex
            self._snapshots[snapshot_id] = FileSnapshot(
                p.relative_to(self.root).as_posix(), data, digest, start, visible_end)
        return {"file": path, "total_lines": len(lines), "content": "\n".join(visible),
                "snapshot_id": snapshot_id, "sha256": digest, "start": start, "end": visible_end,
                "truncated": visible_end < min(end, len(lines))}

    def _expire_snapshots(self, relative: str) -> None:
        for key, snapshot in list(self._snapshots.items()):
            if snapshot.path == relative:
                del self._snapshots[key]

    def replace_lines(self, snapshot_id: str, start: int, end: int, new_text: str) -> dict:
        """Edit displayed lines only if the entire file still matches the read snapshot."""
        if not self.allow_writes:
            raise PermissionError("Pass --allow-writes to enable snapshot editing")
        if not isinstance(snapshot_id, str) or snapshot_id not in self._snapshots:
            raise ValueError("Unknown or expired snapshot_id; call read_file again")
        snapshot = self._snapshots[snapshot_id]
        if type(start) is not int or type(end) is not int or not snapshot.start <= start <= end <= snapshot.end:
            raise ValueError("Edit range must be within this snapshot's displayed lines")
        if not isinstance(new_text, str) or len(new_text) > 60000:
            raise ValueError("new_text must be a string of at most 60000 characters")
        p = self._path(snapshot.path)
        if p.relative_to(self.root).as_posix() != snapshot.path:
            raise ValueError("Snapshot path now resolves to another file; call read_file again")
        current = p.read_bytes()
        if current != snapshot.data:
            self._expire_snapshots(snapshot.path)
            raise ValueError("File changed since the snapshot (whole-file version mismatch); call read_file again")

        # Use cached original lines, not an old_text block retyped by the model.
        text = snapshot.data.decode("utf-8-sig")
        lines = text.splitlines(keepends=True)
        newline = "\r\n" if "\r\n" in text else "\n"
        replacement = new_text.replace("\r\n", "\n").replace("\n", newline)
        if replacement and end < len(lines) and not replacement.endswith(("\n", "\r")):
            replacement += newline  # Whole-line edits must not merge with the following line.
        updated = "".join(lines[:start-1]) + replacement + "".join(lines[end:])
        bom = "\ufeff" if snapshot.data.startswith(codecs.BOM_UTF8) else ""
        result = self._apply_generated_edit(p, bom + text, bom + updated)
        return {"snapshot_id": snapshot_id, "start": start, "end": end, **result}

    def replace_text(self, path: str, old_text: str, new_text: str) -> dict:
        """Replace one exact text block using a program-generated unified diff."""
        if not self.allow_writes:
            raise PermissionError("Pass --allow-writes to enable text replacement")
        p = self._path(path)
        if not p.is_file() or p.stat().st_size > 800_000:
            raise ValueError("File missing or too large")
        if p.suffix.lower() not in TEXT_EXT:
            raise ValueError("Unsupported file type")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError("old_text must be a nonempty string")
        if not isinstance(new_text, str):
            raise ValueError("new_text must be a string")
        if len(old_text) > 60_000 or len(new_text) > 60_000:
            raise ValueError("Each replacement block must be at most 60000 characters")
        relative = p.relative_to(self.root).as_posix()
        if any(char in relative for char in ("\n", "\r", "\t")):
            raise ValueError("Unsupported filename for patch generation")

        # read_file shows logical lines; adapt model-supplied LF to a CRLF file
        # without changing the rest of the file or its trailing-newline state.
        original = p.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in original else "\n"
        old_text = old_text.replace("\r\n", "\n").replace("\n", newline)
        new_text = new_text.replace("\r\n", "\n").replace("\n", newline)
        start = original.find(old_text)
        if start < 0:
            raise ValueError("old_text was not found; read the current file and retry")
        if original.find(old_text, start + 1) >= 0:
            raise ValueError("old_text matches multiple locations; include more surrounding code")
        if old_text == new_text:
            raise ValueError("Replacement makes no change")
        updated = original[:start] + new_text + original[start + len(old_text):]

        return self._apply_generated_edit(p, original, updated)

    def _apply_generated_edit(self, p: Path, original: str, updated: str) -> dict:
        relative = p.relative_to(self.root).as_posix()
        if any(char in relative for char in ("\n", "\r", "\t")):
            raise ValueError("Unsupported filename for patch generation")
        if original == updated:
            raise ValueError("Replacement makes no change")
        lines = difflib.unified_diff(
            original.splitlines(keepends=True), updated.splitlines(keepends=True),
            fromfile="a/" + relative, tofile="b/" + relative,
        )
        # Git requires this marker for changed/context lines without a final LF.
        patch = "".join(line if line.endswith("\n") else
                        line + "\n\\ No newline at end of file\n" for line in lines)
        # Check again immediately before Git, including edits outside the hunk.
        before = original.encode("utf-8")
        if p.read_bytes() != before:
            self._expire_snapshots(relative)
            raise ValueError("File changed while preparing the edit; call read_file again")
        result = self.apply_patch(patch, expected_file=(p, before))
        result["before_sha256"] = hashlib.sha256(before).hexdigest()
        if result["applied"]:
            self._expire_snapshots(relative)
            try:
                after = p.read_bytes()
                result["after_sha256"] = hashlib.sha256(after).hexdigest()
                result["consistent"] = after == updated.encode("utf-8")
            except OSError:
                result["consistent"] = False
            if not result["consistent"]:
                result["error"] = "Post-edit file differs from expected content; call read_file again before proceeding"
        return {"path": relative, "patch": patch, **result}

    def apply_patch(self, patch: str, *, expected_file: tuple[Path, bytes] | None = None) -> dict:
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
            if expected_file is not None:
                expected_path, expected_bytes = expected_file
                try:
                    unchanged = expected_path.read_bytes() == expected_bytes
                except OSError:
                    unchanged = False
                if not unchanged:
                    self._expire_snapshots(expected_path.relative_to(self.root).as_posix())
                    return {"applied": False, "stage": "version_check",
                            "error": "File changed before patch application; call read_file again"}
            # Preserve the patch's line endings instead of inheriting a user's
            # global autocrlf conversion. This affects only this Git invocation.
            args = ["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", "-C", str(self.root), "apply"]
            if extra:
                args.append(extra)
            args.extend(["--whitespace=nowarn", "-"])
            # Text-mode stdin rewrites LF on Windows and can turn CRLF into
            # CRCRLF. A patch must reach Git as the exact UTF-8 bytes generated.
            result = subprocess.run(args, input=patch.encode("utf-8"),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=20)
            if result.returncode != 0:
                return {"applied": False, "stage": extra or "apply",
                        "stderr": result.stderr.decode("utf-8", errors="replace")[:3500]}
        for path in headers:
            if path != "/dev/null":
                self._expire_snapshots(self._path(path).relative_to(self.root).as_posix())
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
        command = [self.test_python, "-m", "pytest", "-q", "--maxfail=1", target]
        log = None
        if self.test_log_dir is not None:
            identifier = "test_" + uuid.uuid4().hex
            log = {"id": identifier, "directory": str(self.test_log_dir / identifier), "status": "running"}
            self.test_log_records.append(log)
        with tempfile.TemporaryDirectory(prefix="repo-agent-pycache-") as cache:
            # Equal-size edits within one timestamp tick can reuse stale .pyc files.
            # Each verification must import the current source, not prior bytecode.
            command[1:1] = ["-X", "pycache_prefix=" + cache]
            metadata = {"command": command, "cwd": str(self.root), "python": self.test_python,
                        "target": target, "timeout_seconds": self.test_timeout,
                        "test_environment": {k: v for k, v in self.test_env.items() if k in
                            {"ANONYMIZED_TELEMETRY", "BROWSER_USE_CONFIG_DIR", "PYTHONPATH", "PYTHONHASHSEED",
                             "PYTHONUTF8", "PYTHONIOENCODING", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}}}
            if log is not None:
                directory = Path(log["directory"])
                try:
                    directory.mkdir(exist_ok=False)
                    self._save_test_metadata(directory, {**metadata, "id": log["id"], "status": "running"})
                except OSError:
                    log["status"] = "write_failed"
                    return {"passed": False, "error": "Test log initialization failed; pytest not started", "test_log": log}
            stdout, stderr, code, timed_out, interrupted, error = b"", b"", None, False, False, None
            try:
                result = subprocess.run(command, cwd=self.root, capture_output=True,
                                        env={**os.environ, **self.test_env},
                                        timeout=self.test_timeout)
                code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out, stdout, stderr = True, exc.stdout, exc.stderr
                error = f"pytest timed out after {self.test_timeout}s"
            except KeyboardInterrupt:
                interrupted, error = True, "pytest interrupted by user"
            except OSError as exc:
                error = "pytest could not start: " + type(exc).__name__
            stdout = stdout.encode("utf-8") if isinstance(stdout, str) else (stdout or b"")
            stderr = stderr.encode("utf-8") if isinstance(stderr, str) else (stderr or b"")
            output = stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")
            answer = {"passed": code == 0 and error is None, "exit_code": code, "timed_out": timed_out,
                      "output": output[-6000:], "output_truncated": len(output) > 6000}
            if error:
                answer["error"] = error
            if log is not None:
                try:
                    streams = {}
                    for name, data in (("stdout", stdout), ("stderr", stderr)):
                        file = directory / (name + ".txt")
                        file.write_bytes(data)
                        streams[name] = {"path": str(file), "bytes": len(data), "sha256": sha256(data)}
                    self._save_test_metadata(directory, {**metadata, "id": log["id"], "status": "recorded",
                        "exit_code": code, "timed_out": timed_out, "interrupted": interrupted,
                        "error": error, **streams})
                    meta_path = directory / "metadata.json"
                    log.update(status="recorded", metadata=str(meta_path), metadata_sha256=sha256(meta_path.read_bytes()), **streams)
                except OSError as exc:
                    log.update(status="write_failed", error_type=type(exc).__name__)
                    answer.update(passed=False, error="Test output logging failed", logging_failed=True)
                answer["test_log"] = dict(log)
            if interrupted:
                raise KeyboardInterrupt
            return answer

    @staticmethod
    def _save_test_metadata(directory: Path, data: dict):
        temporary = directory / "metadata.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(directory / "metadata.json")

    def execute(self, action: str, args: dict) -> dict:
        handlers = {
            "list_files": self.list_files, "search_code": self.search_code,
            "read_file": self.read_file, "apply_patch": self.apply_patch,
            "replace_text": self.replace_text, "replace_lines": self.replace_lines,
            "run_tests": self.run_tests,
        }
        try:
            if action not in handlers or not isinstance(args, dict):
                raise ValueError(f"Unsupported action or args: {action}")
            return {"ok": True, "data": handlers[action](**args)}
        except (OSError, ValueError, TypeError, PermissionError,
                subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
