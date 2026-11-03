"""One-shot worktree preparation. No repository code or Agent execution."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import uuid

from .task_contract import TaskError, load_json, TASK_ID, COMMIT
from .workspace import create_worktree


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def exclusive(path):
    """Nonblocking OS lock, automatically released on process exit."""
    with path.open("a+b") as stream:
        # Windows permits locking beyond EOF. No initialization write: that
        # would race the first holder on a newly created lock file.
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path):
    with path.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Oversized preparation record")
    return load_json(data)


class TaskPreparation:
    def __init__(self, store, projects, workspace_root):
        self.store, self.projects = store, projects
        self.workspace_root = Path(workspace_root).resolve()

    def _path(self, path):
        if path.resolve() != path:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "准备记录位置不符合要求")
        return path

    def _workspace_path(self, task, preparation_id):
        info = self.store.round_info(task["taskId"]) if hasattr(self.store, "round_info") else None
        if info is not None:
            return (self.store.project_workspaces / task["contract"]["projectId"] / "workspaces"
                    / f"{info['taskKey']}_{info['round_id']}_agent")
        return (self.workspace_root / task["contract"]["projectId"] / "workspaces"
                / f"task_{task['taskId']}_{preparation_id}_agent")

    def diagnostics(self, task_id):
        self.store.get(task_id)
        path = self._path(self.store.directory(task_id) / "preparation/diagnostics.json")
        try:
            value = read_json(path)
        except FileNotFoundError:
            return {"available": False, "details": None, "redacted": True}
        from .task_execution import Redactor
        return {"available": True, "details": json.loads(Redactor().public(json.dumps(value, ensure_ascii=False))),
                "redacted": True}

    def _read(self, directory, task_id):
        try:
            record = read_json(self._path(directory / "record.json"))
        except FileNotFoundError:
            return {"schemaVersion": 1, "taskId": task_id, "status": "preparing"}
        if (type(record) is not dict or record.get("schemaVersion") != 1
                or record.get("taskId") != task_id
                or record.get("status") not in ("preparing", "prepared", "preparation_failed")
                or set(record) - {"schemaVersion", "taskId", "status", "preparationId", "workspaceId",
                                  "startedAt", "baseCommit", "isExecutionSandbox", "finishedAt", "error"}):
            raise ValueError("Invalid preparation")
        if "preparationId" in record:
            required = {"schemaVersion", "taskId", "status", "preparationId", "workspaceId", "startedAt", "baseCommit", "isExecutionSandbox"}
            if record["status"] != "preparing":
                required.add("finishedAt")
            if record["status"] == "preparation_failed":
                required.add("error")
            if set(record) != required:
                raise ValueError("Invalid preparation fields")
            if (not isinstance(record["preparationId"], str) or not TASK_ID.fullmatch(record["preparationId"])
                    or record.get("workspaceId") != record["preparationId"]
                    or record.get("isExecutionSandbox") is not False
                    or not isinstance(record.get("baseCommit"), str) or not COMMIT.fullmatch(record["baseCommit"])):
                raise ValueError("Invalid preparation identity")
        elif record["status"] != "preparation_failed" or set(record) != {"schemaVersion", "taskId", "status", "finishedAt", "error"}:
            raise ValueError("Missing preparation identity")
        for field in ("startedAt", "finishedAt"):
            if field in record:
                stamp = record[field]
                if not isinstance(stamp, str) or not stamp.endswith("Z"):
                    raise ValueError("Invalid timestamp")
                datetime.fromisoformat(stamp[:-1] + "+00:00")
        if record["status"] == "preparation_failed":
            error = record.get("error")
            messages = {"PREPARATION_INTERRUPTED": "准备已中断，保留已有材料",
                        "WORKSPACE_IDENTITY_MISMATCH": "工作副本身份无法确认，保留已有材料",
                        "PREPARATION_FAILED": "工作副本准备失败，保留已有材料"}
            if type(error) is not dict or error != {"code": error.get("code"), "message": messages.get(error.get("code"))} or error.get("code") not in messages:
                raise ValueError("Invalid preparation error")
        return record

    def _fail(self, directory, record, code, message):
        record = {**record, "status": "preparation_failed", "finishedAt": now(),
                  "error": {"code": code, "message": message}}
        write_json(self._path(directory / "record.json"), record)
        return record

    def _identity(self, source, workspace):
        def git(root, *args):
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                                  capture_output=True, text=True, encoding="utf-8", timeout=15).stdout.strip()
        def absolute(root, value):
            path = Path(value)
            return str((path if path.is_absolute() else root / path).resolve(strict=True))
        if workspace.resolve(strict=True) != workspace:
            raise ValueError("Workspace redirected")
        top = Path(git(workspace, "rev-parse", "--show-toplevel")).resolve(strict=True)
        git_dir = absolute(workspace, git(workspace, "rev-parse", "--git-dir"))
        common = absolute(workspace, git(workspace, "rev-parse", "--git-common-dir"))
        source_common = absolute(source, git(source, "rev-parse", "--git-common-dir"))
        if top != workspace or git_dir == common or common != source_common:
            raise ValueError("Not an independent worktree of the source")
        if git(workspace, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
            raise ValueError("Not detached")
        return {"source": str(source), "workspace": str(workspace),
                "head": git(workspace, "rev-parse", "HEAD"), "gitDir": git_dir, "commonDir": common}

    def _checkout_config(self, source):
        # Checkout may otherwise invoke user-configured smudge/process programs.
        probe = subprocess.run(["git", "-C", str(source), "config", "--name-only", "--get-regexp",
                                r"^filter\..*\.(smudge|process|required)$"],
                               capture_output=True, text=True, encoding="utf-8", timeout=15)
        if probe.returncode not in (0, 1):
            raise subprocess.CalledProcessError(probe.returncode, probe.args, output=probe.stdout, stderr=probe.stderr)
        config = {"core.hooksPath": os.devnull, "submodule.recurse": "false"}
        for key in probe.stdout.splitlines():
            prefix = key.rsplit(".", 1)[0]
            config.update({prefix + ".smudge": "", prefix + ".process": "", prefix + ".required": "false"})
        return config

    def query(self, task_id, *, prepare=False):
        task = self.store.get(task_id)
        directory = self._path(self.store.directory(task_id) / "preparation")
        lock = self._path(directory.parent / "preparation.lock")
        try:
            with exclusive(lock) as acquired:
                if not acquired:
                    return self._read(directory, task_id)
                if directory.exists():
                    record = self._read(directory, task_id)
                    if record["status"] == "preparing":
                        return self._fail(directory, record, "PREPARATION_INTERRUPTED", "准备已中断，保留已有材料")
                    if record["status"] == "prepared":
                        try:
                            identity = read_json(self._path(directory / "identity.json"))
                            source, workspace = Path(identity["source"]), Path(identity["workspace"])
                            expected = self._workspace_path(task, record['preparationId'])
                            if workspace != expected or self._identity(source, workspace) != identity or identity["head"] != task["contract"]["baseCommit"]:
                                raise ValueError("Identity mismatch")
                        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                            return self._fail(directory, record, "WORKSPACE_IDENTITY_MISMATCH", "工作副本身份无法确认，保留已有材料")
                    return record
                if not prepare:
                    return {"schemaVersion": 1, "taskId": task_id, "status": "not_prepared"}
                directory.mkdir()  # Permanent one-shot claim, never removed.
                preparation_id = uuid.uuid4().hex
                record = {"schemaVersion": 1, "taskId": task_id, "status": "preparing",
                          "preparationId": preparation_id, "workspaceId": preparation_id,
                          "startedAt": now(), "baseCommit": task["contract"]["baseCommit"],
                          "isExecutionSandbox": False}
                write_json(directory / "record.json", record)
                workspace = self._workspace_path(task, preparation_id)
                diagnostic = {"steps": [], "failure": None}
                def step(name):
                    diagnostic["steps"].append({"step": name, "at": now()})
                    write_json(directory / "diagnostics.json", diagnostic)
                try:
                    step("resolve_source")
                    source = self.projects.repository(task["contract"]["projectId"], record["baseCommit"])
                    step("create_workspace_parent")
                    if workspace.resolve() != workspace:
                        raise ValueError("Workspace root redirected")
                    workspace.parent.mkdir(parents=True, exist_ok=True)
                    # Save intended paths before Git, including failed/partial creations.
                    write_json(directory / "identity.json", {"source": str(source), "workspace": str(workspace)})
                    step("inspect_checkout_config")
                    checkout_config = self._checkout_config(source)
                    step("create_worktree")
                    create_worktree(source, workspace, record["baseCommit"],
                                    git_config=checkout_config)
                    step("verify_workspace_identity")
                    identity = self._identity(source, workspace)
                    if identity["head"] != record["baseCommit"]:
                        raise ValueError("Wrong commit")
                    write_json(directory / "identity.json", identity)
                    step("prepared")
                except (OSError, ValueError, TaskError, subprocess.SubprocessError) as exc:
                    from .task_execution import Redactor
                    redactor = Redactor()
                    def text(value):
                        if isinstance(value, bytes):
                            value = value.decode("utf-8", errors="replace")
                        return redactor.private(str(value or ""))
                    failure = {"step": diagnostic["steps"][-1]["step"] if diagnostic["steps"] else "unknown",
                               "errorType": type(exc).__name__, "message": text(exc),
                               "command": [text(v) for v in exc.cmd] if isinstance(getattr(exc, "cmd", None), (list, tuple)) else text(getattr(exc, "cmd", None)),
                               "exitCode": getattr(exc, "returncode", None),
                               "stdout": text(getattr(exc, "stdout", None)), "stderr": text(getattr(exc, "stderr", None))}
                    failure["truncated"] = any(isinstance(v, str) and len(v) > 32000 for v in failure.values())
                    diagnostic["failure"] = {k: v[:32000] if isinstance(v, str) else v for k, v in failure.items()}
                    write_json(directory / "diagnostics.json", diagnostic)
                    return self._fail(directory, record, "PREPARATION_FAILED", "工作副本准备失败，保留已有材料")
                record = {**record, "status": "prepared", "finishedAt": now()}
                write_json(directory / "record.json", record)
                return record
        except TaskError:
            raise
        except OSError as exc:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "准备记录无法读写") from exc
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise TaskError(500, "PREPARATION_RECORD_CORRUPT", "准备记录损坏") from exc
