"""Restricted workspace tools. IMPORTANT: pytest still executes repository code!"""
from __future__ import annotations
import codecs
import ast
from dataclasses import dataclass, field, replace
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from repo_agent.infrastructure.run_evidence import sha256

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".mypy_cache",
             ".pytest_cache", "__pycache__", "dist", "build", ".tox", ".ruff_cache"}
SKIP_FILES = {".env", ".env.local", "id_rsa", "id_ed25519"}
TEXT_EXT = {".py", ".toml", ".md", ".txt", ".yaml", ".yml", ".json", ".ini", ".cfg", ".rst",
            ".html", ".htm", ".css", ".scss", ".sass", ".less",
            ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"}


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    data: bytes | None
    sha256: str
    start: int
    end: int


@dataclass(frozen=True)
class QueryCursor:
    tool: str
    scope: str
    query: str
    files: tuple[str, ...]
    enumeration_errors: int
    index: int = 0
    line: int = 0
    matched_lines_total: int = 0
    skipped_counts: dict[str, int] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)


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
                 test_log_dir: Path | None = None, test_output_filter=None, test_junit: bool = False,
                 test_argv=None,
                 allow_ignored_test_logs: bool = False):
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Repository root must be a directory")
        self.allow_writes = allow_writes
        self.allow_tests = allow_tests
        self.test_timeout = test_timeout
        self.test_env = dict(test_env or {})
        self.test_output_filter = test_output_filter
        self.test_junit = test_junit
        from repo_agent.core.check_execution import validate_check_argv
        self.test_argv = validate_check_argv(test_argv) if test_argv is not None else None
        self.test_log_dir = test_log_dir.resolve() if test_log_dir is not None else None
        if self.test_log_dir is not None:
            if self.test_log_dir == self.root or self.test_log_dir.is_relative_to(self.root):
                ignored = False
                if allow_ignored_test_logs and self.test_log_dir != self.root:
                    check = subprocess.run(['git', '-C', str(self.root), 'check-ignore', '--no-index', '-q', '--',
                                            self.test_log_dir.relative_to(self.root).as_posix()],
                                           capture_output=True, timeout=15)
                    ignored = check.returncode == 0
                if not ignored:
                    raise ValueError("Test logs must be outside the tested worktree or in a verified ignored direct-mode directory")
            self.test_log_dir.mkdir(parents=True, exist_ok=True)
        self.test_log_records: list[dict] = []
        self._snapshots: dict[str, FileSnapshot] = {}
        self._query_cursors: dict[str, QueryCursor] = {}
        self._enumeration_errors = 0
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

    def _iter_files(self, directory: Path, limit: int | None = 1500):
        count = 0
        def enumeration_error(error):
            self._enumeration_errors += 1

        for base, dirs, names in os.walk(directory, followlinks=False, onerror=enumeration_error):
            visible_dirs = []
            for name in sorted(dirs):
                if name.lower() in {value.lower() for value in SKIP_DIRS}:
                    continue
                directory_path = Path(base) / name
                try:
                    info = directory_path.lstat()
                    if (stat.S_ISLNK(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                            or any(directory_path.resolve().is_relative_to(root)
                                   for root in getattr(self, 'excluded_roots', ()))):
                        continue
                except OSError:
                    self._enumeration_errors += 1
                    continue
                visible_dirs.append(name)
            dirs[:] = visible_dirs
            for name in sorted(names):
                path = Path(base) / name
                if name.lower() in {value.lower() for value in SKIP_FILES} or name.lower().startswith('.env'):
                    continue
                if path.suffix.lower() not in TEXT_EXT:
                    continue
                try:
                    info = path.lstat()
                except OSError:
                    self._enumeration_errors += 1
                    continue
                if (not stat.S_ISREG(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    continue
                yield path
                count += 1
                if limit is not None and count >= limit:
                    return

    def _query_path(self, relative: str) -> Path:
        path = self._path(relative)
        lexical = self.root / relative
        if lexical != path:
            raise ValueError("Query path is linked or not canonical")
        for candidate in (lexical, *lexical.parents):
            if candidate == self.root:
                break
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Linked or reparse query path is excluded")
        return path

    def _query_file_visible(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        return (path.suffix.lower() in TEXT_EXT
                and not any(part.lower() in {name.lower() for name in SKIP_DIRS} for part in relative.parts)
                and path.name.lower() not in {name.lower() for name in SKIP_FILES}
                and not path.name.lower().startswith(".env"))

    def _query_state(self, tool: str, scope: str, query: str, cursor: str) -> QueryCursor:
        if not isinstance(cursor, str) or len(cursor) > 64:
            raise ValueError("cursor must be a string of at most 64 characters")
        target = self._query_path(scope)
        self._enumeration_errors = 0
        if target.is_dir():
            candidates = self._iter_files(target, limit=None)
        elif tool == "search_code" and target.is_file() and self._query_file_visible(target):
            candidates = iter([target])
        else:
            raise ValueError("Query scope must be a visible text file or directory" if tool == "search_code"
                             else "subdir must be a directory")
        files = []
        for candidate in candidates:
            relative = candidate.relative_to(self.root).as_posix()
            try:
                self._query_path(relative)
            except OSError:
                self._enumeration_errors += 1
                continue
            except ValueError:
                continue
            if not self._query_file_visible(candidate):
                continue
            files.append(relative)
        manifest = tuple(sorted(set(files)))
        if not cursor:
            return QueryCursor(tool, scope, query, manifest, self._enumeration_errors,
                               skipped_counts={"too_large": 0, "read_errors": 0,
                                               "enumeration_errors": self._enumeration_errors})
        saved = self._query_cursors.get(cursor)
        if saved is None or (saved.tool, saved.scope, saved.query) != (tool, scope, query):
            raise ValueError("Unknown, expired or mismatched cursor; restart the query")
        if saved.files != manifest or saved.enumeration_errors != self._enumeration_errors:
            raise ValueError("Query candidate paths changed; restart the query without a cursor")
        for relative, digest in saved.versions.items():
            try:
                current = self._query_path(relative)
                with current.open("rb") as stream:
                    data = stream.read(700001)
            except (OSError, ValueError):
                raise ValueError("Previously scanned file is unavailable; restart the query") from None
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("File changed since the search page; restart the query without a cursor")
        return saved

    def _save_query_cursor(self, state: QueryCursor) -> str:
        token = "cursor_" + uuid.uuid4().hex
        if len(self._query_cursors) >= 32:
            del self._query_cursors[next(iter(self._query_cursors))]
        self._query_cursors[token] = state
        return token

    @staticmethod
    def _page_limit(limit: int, maximum: int) -> None:
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError(f"limit must be an integer in 1..{maximum}")

    def _query_visibility(self) -> dict:
        return {"excluded_dirs": sorted(SKIP_DIRS), "excluded_files": sorted(SKIP_FILES),
                "env_prefix_excluded": True, "links_excluded": True,
                "runtime_roots_excluded": bool(getattr(self, "excluded_roots", ()))}

    def list_files(self, subdir: str = ".", cursor: str = "", limit: int = 500) -> dict:
        self._page_limit(limit, 500)
        state = self._query_state("list_files", subdir, "", cursor)
        paths = []
        index, size = state.index, 0
        stop = "exhausted"
        while index < len(state.files):
            relative = state.files[index]
            extra = len(json.dumps(relative, ensure_ascii=False)) + 2
            if extra > 25000:
                raise ValueError("Filename exceeds the output budget; narrow the directory scope")
            if paths and size + extra > 25000:
                stop = "output_limit"
                break
            paths.append(relative)
            size += extra
            index += 1
            if len(paths) >= limit and index < len(state.files):
                stop = "page_limit"
                break
        more = index < len(state.files)
        return {"files": paths, "returned": len(paths), "limit": limit,
                "scope": "supported_text_files", "subdir": subdir,
                "supported_extensions": sorted(TEXT_EXT), "has_more": more,
                "next_cursor": self._save_query_cursor(replace(state, index=index)) if more else None,
                "complete": not more and not state.enumeration_errors, "stop_reason": stop,
                "skipped_counts": dict(state.skipped_counts), "consistency": "version_checked_pages",
                "visibility": self._query_visibility()}

    def search_code(self, query: str, path: str = ".", cursor: str = "", limit: int = 50) -> dict:
        if not isinstance(query, str) or not (1 <= len(query) <= 100):
            raise ValueError("query length must be 1..100")
        self._page_limit(limit, 50)
        state = self._query_state("search_code", path, query, cursor)
        hits, skipped, versions = [], dict(state.skipped_counts), dict(state.versions)
        index, line_index, scanned, attempted, size = state.index, state.line, 0, 0, 0
        stop = "exhausted"
        while index < len(state.files):
            if attempted >= 1500:
                stop = "file_limit"
                break
            relative = state.files[index]
            attempted += 1
            try:
                candidate = self._query_path(relative)
                if candidate.stat().st_size > 700000:
                    skipped["too_large"] += 1
                    index, line_index = index + 1, 0
                    continue
                with candidate.open("rb") as stream:
                    data = stream.read(700001)
            except OSError:
                skipped["read_errors"] += 1
                index, line_index = index + 1, 0
                continue
            if len(data) > 700000:
                skipped["too_large"] += 1
                index, line_index = index + 1, 0
                continue
            versions[relative] = hashlib.sha256(data).hexdigest()
            lines = io.StringIO(data.decode("utf-8", errors="replace"), newline=None).readlines()
            while line_index < len(lines):
                original_line = lines[line_index]
                text = original_line.rstrip()
                if query.casefold() in original_line.casefold():
                    hit = {"file": relative, "line": line_index + 1, "text": text[:240],
                           "text_truncated": len(text) > 240}
                    extra = len(json.dumps(hit, ensure_ascii=False))
                    if extra > 25000:
                        raise ValueError("Match path exceeds the output budget; narrow the directory scope")
                    if hits and size + extra > 25000:
                        stop = "output_limit"
                        break
                    hits.append(hit)
                    size += extra
                line_index += 1
                if len(hits) >= limit and (line_index < len(lines) or index + 1 < len(state.files)):
                    stop = "match_limit"
                    break
            if line_index >= len(lines):
                scanned += 1
                index, line_index = index + 1, 0
            if stop != "exhausted":
                break
        more = index < len(state.files)
        total = state.matched_lines_total + len(hits)
        next_state = replace(state, index=index, line=line_index, matched_lines_total=total,
                             skipped_counts=skipped, versions=versions)
        return {"matches": hits, "truncated": more, "path": path,
                "scope": "supported_text_files", "supported_extensions": sorted(TEXT_EXT),
                "limit": limit, "scan_complete": not more and not any(skipped.values()),
                "stop_reason": stop, "scanned_files": scanned, "matched_lines_total": total,
                "skipped_counts": skipped, "has_more": more,
                "next_cursor": self._save_query_cursor(next_state) if more else None,
                "consistency": "version_checked_pages", "visibility": self._query_visibility()}

    def read_file(self, path: str, start: int = 1, end: int = 160) -> dict:
        p = self._path(path)
        if not p.is_file():
            raise ValueError("File missing")
        if p.suffix.lower() not in TEXT_EXT:
            raise ValueError("Unsupported file type")
        start, end = int(start), int(end)
        if start < 1 or end < start or end - start > 200:
            raise ValueError("Invalid line range (max 201 lines)")
        if p.stat().st_size > 800_000:
            before = p.stat()
            digest = hashlib.sha256()
            visible, total, size, display_capped = [], 0, 0, False
            decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
            with p.open('rb') as stream:
                while True:
                    raw = stream.readline(1_000_001)
                    if not raw:
                        break
                    if len(raw) > 1_000_000 and not raw.endswith(b'\n'):
                        raise ValueError('Line too long to read safely')
                    digest.update(raw)
                    total += 1
                    decoded = decoder.decode(raw, final=False)
                    line_text = decoded.removesuffix('\n').removesuffix('\r')
                    if any(c in line_text for c in '\r\v\f\x1c\x1d\x1e\x85\u2028\u2029'):
                        raise ValueError('Large files require LF or CRLF line endings')
                    if start <= total <= end and not display_capped:
                        line = f'{total}: {line_text}'
                        extra = len(line) + bool(visible)
                        if size + extra <= 25000:
                            visible.append(line)
                            size += extra
                        elif not visible:
                            raise ValueError('Line too long to display completely')
                        else:
                            display_capped = True
                decoder.decode(b'', final=True)
            after = p.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError('File changed while reading; try again')
            verification = hashlib.sha256()
            with p.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    verification.update(chunk)
            if verification.digest() != digest.digest():
                raise ValueError('File changed while reading; try again')
            visible_end = start + len(visible) - 1
            snapshot_id = None
            if visible:
                snapshot_id = 'snapshot_' + uuid.uuid4().hex
                self._snapshots[snapshot_id] = FileSnapshot(
                    p.relative_to(self.root).as_posix(), None, digest.hexdigest(), start, visible_end)
            return {'file': path, 'total_lines': total, 'content': '\n'.join(visible),
                    'snapshot_id': snapshot_id, 'sha256': digest.hexdigest(), 'start': start,
                    'end': visible_end, 'empty': False, 'truncated': visible_end < min(end, total)}
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
        empty = not lines and start == 1
        if empty:
            visible_end = 1  # A virtual insertion range, not a displayed source line.
        if visible or empty:
            snapshot_id = "snapshot_" + uuid.uuid4().hex
            self._snapshots[snapshot_id] = FileSnapshot(
                p.relative_to(self.root).as_posix(), data, digest, start, visible_end)
        return {"file": path, "total_lines": len(lines), "content": "\n".join(visible),
                "snapshot_id": snapshot_id, "sha256": digest, "start": start, "end": visible_end,
                "empty": not lines, "truncated": visible_end < min(end, len(lines))}

    def _creation_path(self, relative: str) -> Path:
        # Reject lexical aliases before resolution, including Windows ADS/device names.
        if (not isinstance(relative, str) or not relative
                or any(ord(c) < 32 or c in '\\:*?"<>|' for c in relative)):
            raise ValueError("Invalid creation path")
        parts = relative.split("/")
        reserved = {"con", "prn", "aux", "nul"} | {f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10)}
        for part in parts:
            if (not part or part in (".", "..") or part.endswith((" ", "."))
                    or part.lower() in {name.lower() for name in SKIP_DIRS | SKIP_FILES}
                    or part.lower().startswith(".env") or part.lower().split(".")[0] in reserved):
                raise ValueError("Creation path is excluded or invalid")
        candidate = self._path(relative)
        current = self.root
        for part in parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Linked or reparse creation path is excluded")
        if candidate.suffix.lower() not in TEXT_EXT:
            raise ValueError("Unsupported file type")
        return candidate

    def create_file(self, path: str, content: str) -> dict:
        if not self.allow_writes:
            raise PermissionError("Pass --allow-writes to enable file creation")
        if not isinstance(content, str) or len(content) > 60000 or "\0" in content:
            raise ValueError("content must be text of at most 60000 characters without NUL")
        data = content.encode("utf-8")  # Exact UTF-8 bytes, with no implicit BOM/newline conversion.
        p = self._creation_path(path)
        if p.exists():
            raise FileExistsError("Creation target already exists")
        p.parent.mkdir(parents=True, exist_ok=True)
        p = self._creation_path(path)  # Recheck parents immediately before exclusive creation.
        with p.open("xb") as stream:
            try:
                stream.write(data)
                stream.flush()
            except OSError:
                # Remove our partial output only while it still names the file we opened.
                if p.stat() == os.fstat(stream.fileno()):
                    stream.close()
                    p.unlink()
                raise
        self._expire_snapshots(path)
        result = {"path": path, "applied": True, "created": True, "bytes_written": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "consistent": False}
        try:
            after = p.read_bytes()
            result.update(after_sha256=hashlib.sha256(after).hexdigest(), consistent=after == data)
        except OSError:
            pass
        if not result["consistent"]:
            result["error"] = "Post-creation file differs from expected content; call read_file again before proceeding"
        return self._syntax_feedback(p, result)

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
        if snapshot.data is None:
            return self._replace_large_lines(p, snapshot_id, snapshot, start, end, new_text)
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
        return {"snapshot_id": snapshot_id, "start": start, "end": end,
                **self._syntax_feedback(p, result)}

    def _syntax_feedback(self, path, result):
        if not result.get('applied') or path.suffix.lower() != '.py':
            return result
        # This is advisory: the target project's interpreter may differ.
        try:
            with path.open('rb') as stream:
                source = stream.read(2_000_001)
            if len(source) > 2_000_000:
                result['syntax_check'] = {'status': 'not_checked', 'reason': 'file exceeds advisory check limit'}
            else:
                ast.parse(source, filename=str(path))
                result['syntax_check'] = {'status': 'passed', 'interpreter': sys.version.split()[0], 'advisory': True}
        except SyntaxError as exc:
            result['syntax_check'] = {'status': 'failed', 'line': exc.lineno, 'column': exc.offset,
                                      'message': exc.msg, 'interpreter': sys.version.split()[0], 'advisory': True}
        except (OSError, ValueError, UnicodeError) as exc:
            result['syntax_check'] = {'status': 'not_checked', 'reason': str(exc)[:300]}
        return result

    def _replace_large_lines(self, path, snapshot_id, snapshot, start, end, new_text):
        editable = getattr(self, 'editable', None)
        if callable(editable) and not editable(snapshot.path):
            raise PermissionError('Edit exceeds allowed business-code scope')
        temporary = None
        original_digest = hashlib.sha256()
        changed_digest = hashlib.sha256()
        old_lines = []
        inserted = False
        try:
            with path.open('rb') as source, tempfile.NamedTemporaryFile(
                    mode='wb', dir=path.parent, prefix='.agent-edit-', delete=False) as output:
                temporary = Path(output.name)
                line_no = 0
                while True:
                    raw = source.readline(1_000_001)
                    if not raw:
                        break
                    if len(raw) > 1_000_000 and not raw.endswith(b'\n'):
                        raise ValueError('Line too long to edit safely')
                    line_no += 1
                    original_digest.update(raw)
                    if start <= line_no <= end:
                        old_lines.append(raw)
                        if not inserted:
                            newline = '\r\n' if raw.endswith(b'\r\n') else '\n'
                            replacement = new_text.replace('\r\n', '\n').replace('\n', newline)
                            if replacement and not replacement.endswith(('\n', '\r')) and (end < snapshot.end or raw.endswith(b'\n')):
                                replacement += newline
                            data = replacement.encode('utf-8')
                            if line_no == 1 and raw.startswith(codecs.BOM_UTF8):
                                data = codecs.BOM_UTF8 + data
                            output.write(data)
                            changed_digest.update(data)
                            inserted = True
                    else:
                        output.write(raw)
                        changed_digest.update(raw)
                output.flush()
                os.fsync(output.fileno())
            if not inserted or original_digest.hexdigest() != snapshot.sha256:
                self._expire_snapshots(snapshot.path)
                raise ValueError('File changed since the snapshot; call read_file again')
            current = hashlib.sha256()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(65536), b''):
                    current.update(chunk)
            if current.hexdigest() != snapshot.sha256:
                self._expire_snapshots(snapshot.path)
                raise ValueError('File changed while preparing the edit; call read_file again')
            if changed_digest.digest() == original_digest.digest():
                raise ValueError('Replacement makes no change')
            os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
            temporary = None
            self._expire_snapshots(snapshot.path)
            original = b''.join(old_lines).decode('utf-8-sig').splitlines(keepends=True)
            patch = ''.join(difflib.unified_diff(original, new_text.splitlines(keepends=True),
                fromfile='a/' + snapshot.path, tofile='b/' + snapshot.path))
            result = {'path': snapshot.path, 'paths': [snapshot.path], 'snapshot_id': snapshot_id, 'start': start, 'end': end,
                      'applied': True, 'consistent': True, 'before_sha256': snapshot.sha256,
                      'after_sha256': changed_digest.hexdigest(), 'patch': patch,
                      'patch_scope': 'selected lines'}
            return self._syntax_feedback(path, result)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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

    def run_tests(self, target: str = ".", *, scratch=None, changed_python=(),
                  source_version=None, environment_version=None) -> dict:
        from repo_agent.core.check_execution import (
            REPORT_LIMIT, detect_repository_runner, execution_receipt, interpret_execution,
            test_command, validate_sympy_selector,
        )

        if not self.allow_tests:
            raise PermissionError("Pass --allow-tests to enable running repo tests")
        if not isinstance(target, str) or not target or len(target) > 220:
            raise ValueError("Invalid test target")
        # no pytest option injection, no shell, no external path.
        base = target.split("::", 1)[0]
        if base.startswith("-") or any(c in target for c in ("\n", "\r", "\0")):
            raise ValueError("Invalid test target")
        if scratch is None:
            target_path = self._path(base)
        else:
            target_path = Path(scratch) / target.removeprefix('.agent-checks/')
        if source_version is None:
            source_version = {'target': target, 'snapshot_sha256': None}
            if target_path.is_file():
                source_version['test_sha256'] = sha256(target_path.read_bytes())
            if (self.root / '.git').exists():
                try:
                    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.root,
                                          capture_output=True, timeout=5)
                    if head.returncode == 0:
                        source_version['head'] = head.stdout.decode('ascii', errors='replace').strip()
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if environment_version is None:
            stamp = Path(self.test_python).stat()
            identity = [self.test_python, stamp.st_size, stamp.st_mtime_ns, self.test_argv,
                        {**os.environ, **self.test_env}]
            environment_version = sha256(json.dumps(identity, sort_keys=True).encode())
        argv = self.test_argv if scratch is None else None
        repo = 'command' if argv is not None else detect_repository_runner(self.root)
        if scratch is None and argv is None:
            selectors = target.split('::')[1:]
            if repo == 'sympy/sympy' and selectors:
                try:
                    validate_sympy_selector(self._path(base).read_bytes(), target)
                except (OSError, ValueError):
                    repo = 'pytest'
            if repo == 'django/django' and (base not in ('.', 'tests') and not base.startswith('tests/')
                                            or not all(node.isidentifier() for node in selectors)):
                repo = 'pytest'
        config = {'repo': repo, 'target': target, 'temporary': scratch is not None,
                  'argv': argv,
                  'python': self.test_python, 'workspace': str(self.root),
                  'pytestOptions': ['--maxfail=1']}
        if scratch is not None:
            config['scratch'] = str(scratch)
        log = None
        if self.test_log_dir is not None:
            identifier = "test_" + uuid.uuid4().hex
            log = {"id": identifier, "directory": str(self.test_log_dir / identifier), "status": "running"}
            self.test_log_records.append(log)
        with tempfile.TemporaryDirectory(prefix="repo-agent-pycache-") as cache:
            # Equal-size edits within one timestamp tick can reuse stale .pyc files.
            # Each verification must import the current source, not prior bytecode.
            config['cache'] = cache
            if (self.test_junit and repo == 'pytest' and scratch is None) or (argv and '{report}' in argv):
                config['report'] = str(Path(log["directory"] if log else cache) / 'report.xml')
            command = test_command(config)
            metadata = {"command": command, "cwd": str(self.root), "python": self.test_python,
                        "target": target, "runner": repo, "timeout_seconds": self.test_timeout,
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
                    error = 'Test log initialization failed; test command not started'
                    receipt = execution_receipt(command, self.root, stage='setup', error=error,
                                                source=source_version, logs={'status': 'write_failed'})
                    return {**interpret_execution(config, receipt, ''), 'error': error, 'test_log': log}
            stdout, stderr, code, timed_out, interrupted, error = b"", b"", None, False, False, None
            syntax_failed = False
            stage = 'syntax' if changed_python else 'launch'
            started = time.monotonic()
            try:
                environment = {**os.environ, **self.test_env}
                if changed_python:
                    compile_sources = ('from pathlib import Path; import sys\n'
                                       'for name in sys.argv[1:]:\n'
                                       '    compile(Path(name).read_bytes(), name, "exec")')
                    metadata['syntax_command'] = [self.test_python, '-c', compile_sources, *changed_python]
                    result = subprocess.run(metadata['syntax_command'],
                                            cwd=self.root, capture_output=True, env=environment,
                                            timeout=self.test_timeout)
                    syntax_failed = result.returncode != 0
                if not syntax_failed:
                    stage = 'launch'
                    result = subprocess.run(command, cwd=self.root, capture_output=True, env=environment,
                                            timeout=max(0.001, self.test_timeout - (time.monotonic() - started)))
                    stage = 'tests'
                code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out, stdout, stderr = True, exc.stdout, exc.stderr
                if stage == 'launch':
                    stage = 'tests'
                error = f"Test command timed out after {self.test_timeout}s"
            except KeyboardInterrupt:
                interrupted, error = True, "Test execution interrupted by user"
            except OSError as exc:
                error = "Could not start command: " + type(exc).__name__
            elapsed_seconds = time.monotonic() - started
            stdout = stdout.encode("utf-8") if isinstance(stdout, str) else (stdout or b"")
            stderr = stderr.encode("utf-8") if isinstance(stderr, str) else (stderr or b"")
            if self.test_output_filter is not None:
                stdout = self.test_output_filter(stdout)
                stderr = self.test_output_filter(stderr)
            output = stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")
            report = None
            if config.get('report'):
                try:
                    with Path(config['report']).open('rb') as stream:
                        report = stream.read(REPORT_LIMIT + 1)
                    if len(report) <= REPORT_LIMIT and self.test_output_filter:
                        report = self.test_output_filter(report)
                        Path(config['report']).write_bytes(report)
                except OSError:
                    report = None
            receipt_logs = {'status': 'not_configured'}
            if log is not None:
                try:
                    streams = {}
                    for name, data in (("stdout", stdout), ("stderr", stderr)):
                        file = directory / (name + ".txt")
                        file.write_bytes(data)
                        streams[name] = {"path": str(file), "bytes": len(data), "sha256": sha256(data)}
                    receipt_logs = {'status': 'partial' if timed_out or interrupted else 'complete', **streams}
                except OSError as exc:
                    log.update(status="write_failed", error_type=type(exc).__name__)
                    receipt_logs = {'status': 'write_failed'}
            receipt = execution_receipt(command, self.root, exit_code=code, timed_out=timed_out,
                                        interrupted=interrupted, error=error, stage=stage,
                                        elapsed_seconds=elapsed_seconds, source=source_version,
                                        environment={'python': self.test_python, 'fingerprint': environment_version,
                                                     **metadata['test_environment']}, logs=receipt_logs)
            if log is not None and log['status'] != 'write_failed':
                try:
                    self._save_test_metadata(directory, {**metadata, "id": log["id"], "status": "recorded",
                        "exit_code": code, "timed_out": timed_out, "interrupted": interrupted,
                        "test_started": stage == 'tests', "execution": receipt, "error": error, **streams})
                    meta_path = directory / "metadata.json"
                    log.update(status="recorded", metadata=str(meta_path), metadata_sha256=sha256(meta_path.read_bytes()), **streams)
                except OSError as exc:
                    log.update(status="write_failed", error_type=type(exc).__name__)
            parsed = interpret_execution(config, receipt, output, report=report)
            if syntax_failed:
                is_syntax = 'SyntaxError:' in output or 'IndentationError:' in output or 'TabError:' in output
                parsed.update(passed=False, check_status='failed' if is_syntax else 'unknown',
                              failure_stage='candidate_syntax' if is_syntax else 'infrastructure',
                              failure_kind='syntax_error' if is_syntax else 'execution_error',
                              failure_evidence=[output.strip()[-700:]], check_kind='syntax_check')
            if error:
                parsed.update(passed=False, check_status='unknown',
                              failure_detail=error)
            answer = {**parsed, "exit_code": code, "timed_out": timed_out, "runner": repo,
                      "test_started": stage == 'tests',
                      "output": output[-6000:], "output_truncated": len(output) > 6000}
            if error:
                answer["error"] = error
            if log is not None:
                if log['status'] == 'write_failed':
                    receipt['logs']['status'] = 'write_failed'
                    answer.update(passed=False, check_status='unknown', error="Test output logging failed",
                                  logging_failed=True, failure_stage='infrastructure',
                                  failure_detail='Test output logging failed')
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
            "create_file": self.create_file,
            "run_tests": self.run_tests,
        }
        try:
            if action not in handlers or not isinstance(args, dict):
                raise ValueError(f"Unsupported action or args: {action}")
            return {"ok": True, "data": handlers[action](**args)}
        except (OSError, ValueError, TypeError, PermissionError,
                subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
