"""Background ordinary-task execution using the existing Agent/Provider loop."""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid

from .agent import Agent
from .config import read_dotenv
from .provider import OpenAICompatibleProvider
from .run_evidence import archive_agent_source
from .task_contract import TASK_ID, TaskError, _relative_path
from .task_execution_tools import TaskRepoTools, protected
from .task_preparation import exclusive, now, read_json, write_json


def git(root, *args):
    probe = subprocess.run(["git", "-C", str(root), "config", "--name-only", "--get-regexp",
                            r"^filter\..*\.(clean|smudge|process|required)$"], capture_output=True, timeout=15)
    if probe.returncode not in (0, 1):
        raise ValueError("Cannot inspect Git filters")
    options = ["-c", "core.hooksPath=" + os.devnull]
    for key in probe.stdout.decode("utf-8").splitlines():
        prefix = key.rsplit(".", 1)[0]
        for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            options.extend(["-c", prefix + "." + suffix + "=" + value])
    return subprocess.run(["git", "--no-optional-locks", *options, "-c", "core.fsmonitor=false",
                           "-C", str(root), *args], capture_output=True, check=True, timeout=30).stdout


class Redactor:
    def __init__(self):
        local = read_dotenv()
        self.secrets = {v for source in (os.environ, local) for k, v in source.items()
                        if re.search(r"key|token|secret|password|credential", k, re.I) and len(v) >= 4}

    def private(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def public(self, text):
        text = self.private(text)
        text = re.sub(r"(?i)[a-z]:[\\/][^\s\"'<>]*", "[LOCAL_PATH]", text)
        text = re.sub(r"(?<![\w.])/(?!dev/null\b)[^\s\"'<>]+", "[LOCAL_PATH]", text)
        return text

    def output(self, raw):
        return self.private(raw.decode("utf-8", errors="replace")).encode("utf-8")


class RecordedAgent(Agent):
    """Only recording hooks change; all decisions and tools use Agent.run."""
    def __init__(self, *args, recorder, redactor, **kwargs):
        self.recorder, self.redactor = recorder, redactor
        super().__init__(*args, **kwargs)

    def _record(self, item):
        clean = json.loads(self.redactor.private(json.dumps(item, ensure_ascii=False)))
        super()._record(clean)
        event = {"type": item["type"]}
        for key in ("step", "action", "attempt", "outcome"):
            if key in item:
                event[key] = item[key]
        if item["type"] == "observation":
            event["ok"] = item["result"]["ok"]
            result = item["result"]
            data = result.get("data", {})
            feedback = {"kind": item["action"], "status": "ok" if result["ok"] else "error",
                        "message": "工具操作完成" if result["ok"] else self.redactor.public(result.get("error", "工具操作失败"))}
            if item["action"] == "run_tests" and result["ok"]:
                feedback.update(status=data["check_status"], passed=data["passed"], exitCode=data.get("exit_code"),
                                timedOut=data.get("timed_out", False), counts=data.get("counts"),
                                message=data.get("check_reason") or "固定目标测试已通过")
            for original, key in (("file", "path"), ("returned", "returned"), ("total_lines", "totalLines"),
                                  ("paths", "paths"), ("applied", "applied")):
                if original in data:
                    feedback[key] = data[original]
            if "applied" in data and not data["applied"]:
                feedback.update(status="error", message=self.redactor.public(data.get("error") or data.get("stderr") or "修改未应用"))
            if "matches" in data:
                feedback["returned"] = len(data["matches"])
            feedback.update(messageTruncated=len(feedback["message"]) > 400, redacted=True)
            feedback["message"] = feedback["message"][:400]
            event["feedback"] = feedback
        if item["type"] == "model_request_attempt":
            event["type"] = "model_attempt"
        self.recorder(event)


class TaskExecution:
    def __init__(self, store, projects, preparation, *, model_factory=None):
        self.store, self.projects, self.preparation = store, projects, preparation
        self.model_factory = model_factory or OpenAICompatibleProvider
        self.source = "injected_test_model" if model_factory is not None else "configured_provider"
        self.threads = []

    def _path(self, path):
        if path.resolve() != path:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "执行材料位置不符合要求")
        return path

    def _directory(self, task_id):
        return self._path(self.store.root / f"task_{task_id}" / "execution")

    @contextmanager
    def _io(self):
        try:
            yield
        except TaskError:
            raise
        except OSError as exc:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "执行材料无法读写") from exc
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise TaskError(500, "EXECUTION_RECORD_CORRUPT", "执行记录损坏") from exc

    def _read_state(self, task_id):
        directory = self._directory(task_id)
        if not directory.exists():
            return None
        try:
            state = read_json(self._path(directory / "state.json"))
        except FileNotFoundError:
            return {"schemaVersion": 1, "taskId": task_id, "status": "unknown", "source": self.source,
                    "canStart": False, "startBlockReason": "ALREADY_STARTED", "isExecutionSandbox": False,
                    "liveness": "unconfirmed", "resultAvailable": False, "stopReason": None}
        required = {"schemaVersion", "taskId", "executionId", "status", "source", "canStart", "startBlockReason",
                    "isExecutionSandbox", "phase", "activity", "steps", "maxSteps", "startedAt", "updatedAt",
                    "finishedAt", "liveness", "stopReason", "resultAvailable"}
        if (type(state) is not dict or set(state) != required or state["schemaVersion"] != 1
                or state["taskId"] != task_id or not TASK_ID.fullmatch(state["executionId"])
                or state["status"] not in ("running", "finalizing", "terminated", "unknown")
                or state["source"] not in ("configured_provider", "injected_test_model")):
            raise ValueError("Invalid execution state")
        return state

    def _owner(self):
        path = self._path(self.store.root / "execution-owner.json")
        if not path.exists():
            return None
        owner = read_json(path)
        if (type(owner) is not dict or set(owner) != {"taskId", "executionId"}
                or any(not isinstance(v, str) or not TASK_ID.fullmatch(v) for v in owner.values())):
            raise ValueError("Invalid execution owner")
        state = self._read_state(owner["taskId"])
        if state is None or state.get("executionId") != owner["executionId"] or state["status"] != "terminated":
            return owner
        return None

    def _workspace(self, task):
        task_id = task["taskId"]
        if self.preparation.query(task_id)["status"] != "prepared":
            raise TaskError(409, "PREPARATION_REQUIRED", "工作副本尚未准备成功")
        identity_path = self._path(self.store.root / f"task_{task_id}" / "preparation/identity.json")
        identity = read_json(identity_path)
        workspace, source = Path(identity["workspace"]), Path(identity["source"])
        if self.preparation._identity(source, workspace) != identity or identity["head"] != task["contract"]["baseCommit"]:
            raise TaskError(409, "WORKSPACE_UNAVAILABLE", "工作副本身份不匹配")
        return workspace, identity

    def _ready(self, task):
        try:
            workspace, identity = self._workspace(task)
            if git(workspace, "status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=all"):
                return "WORKSPACE_DIRTY", None
            config = self.projects.execution_config(task["contract"]["projectId"])
            # Inspect without contacting a model. Optional configuration is frozen now.
            redactor = Redactor()
            model = None
            if self.source == "configured_provider":
                try:
                    model = OpenAICompatibleProvider()  # Configuration only, no network request.
                except (ValueError, RuntimeError):
                    return "EXECUTION_CONFIG_UNAVAILABLE", None
            return None, (workspace, identity, config, redactor, model)
        except TaskError as exc:
            return (exc.code if exc.code in ("PREPARATION_REQUIRED", "EXECUTION_CONFIG_UNAVAILABLE") else "WORKSPACE_UNAVAILABLE"), None
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return "WORKSPACE_UNAVAILABLE", None

    def get(self, task_id, *, check_readiness=True):
        task = self.store.get(task_id)
        with self._io():
            state = self._read_state(task_id)
            if state is None and not check_readiness:
                return {"status": "not_started", "resultAvailable": False}
            with exclusive(self._path(self.store.root / "execution-global.lock")) as acquired:
                if state is not None:
                    if state["status"] in ("running", "finalizing"):
                        owner = self._owner()
                        if acquired or owner is None or owner["taskId"] != task_id:
                            state = {**state, "status": "unknown", "liveness": "unconfirmed", "activity": "unconfirmed"}
                    if state["status"] == "terminated":
                        state = {**state, "resultAvailable": self._path(self._directory(task_id) / "result.json").is_file()}
                    return state
                reason = "EXECUTION_BUSY" if not acquired else "EXECUTION_UNRESOLVED" if self._owner() else None
                if reason is None:
                    reason, _ = self._ready(task)
                return {"schemaVersion": 1, "taskId": task_id, "status": "not_started", "canStart": reason is None,
                        "startBlockReason": reason, "source": self.source, "isExecutionSandbox": False}

    def start(self, task_id, value):
        if type(value) is not dict or value:
            raise TaskError(400, "VALIDATION_ERROR", "启动请求须为空 JSON 对象")
        task = self.store.get(task_id)
        with self._io():
            if self._directory(task_id).exists():
                return 200, self.get(task_id)
            lock = exclusive(self._path(self.store.root / "execution-global.lock"))
            acquired = lock.__enter__()
            handed_over = False
            try:
                if self._directory(task_id).exists():
                    return 200, self._read_state(task_id)
                if not acquired:
                    raise TaskError(409, "EXECUTION_BUSY", "已有任务正在执行")
                if self._owner():
                    raise TaskError(409, "EXECUTION_UNRESOLVED", "旧执行尚未核实，禁止再次启动")
                reason, context = self._ready(task)
                if reason:
                    raise TaskError(409, reason, "任务当前不可启动，请查询执行状态")
                directory = self._directory(task_id)
                directory.mkdir()
                stamp, execution_id = now(), uuid.uuid4().hex
                state = {"schemaVersion": 1, "taskId": task_id, "executionId": execution_id,
                         "status": "running", "source": self.source, "canStart": False,
                         "startBlockReason": "ALREADY_STARTED", "isExecutionSandbox": False,
                         "phase": "agent", "activity": "model_or_tool", "steps": 0,
                         "maxSteps": task["contract"]["maxSteps"], "startedAt": stamp, "updatedAt": stamp,
                         "finishedAt": None, "liveness": "confirmed", "stopReason": None, "resultAvailable": False}
                write_json(self._path(directory / "state.json"), state)
                write_json(self._path(self.store.root / "execution-owner.json"), {"taskId": task_id, "executionId": execution_id})
                thread = threading.Thread(target=self._worker, args=(task, state.copy(), context, lock), daemon=True,
                                          name="ordinary-task-" + execution_id)
                thread.start()
                handed_over = True
                self.threads.append(thread)
                return 202, state
            finally:
                if not handed_over:
                    lock.__exit__(None, None, None)

    def _snapshot(self, workspace):
        result = {}
        for base, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in list(dirs) + files:
                path = Path(base) / name
                relative = path.relative_to(workspace).as_posix()
                if relative == ".git":
                    continue
                if path.resolve() != path:
                    result[relative] = "LINK"
                    if name in dirs:
                        dirs.remove(name)
                elif path.is_file():
                    result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def _review(self, workspace, identity, baseline, tools):
        after = self._snapshot(workspace)
        changed = sorted(p for p in baseline.keys() | after.keys() if baseline.get(p) != after.get(p))
        outside, protected_paths = [], []
        for path in changed:
            try:
                allowed = tools.editable(path) and after.get(path) != "LINK"
            except (ValueError, OSError):
                allowed = False
            if not allowed:
                outside.append(path)
            if protected(path, tools.contract["checkTargets"]) or not _relative_path(path):
                protected_paths.append(path)
        identity_ok = self.preparation._identity(Path(identity["source"]), workspace) == identity
        metadata_ok = self._git_metadata(workspace, identity) == tools.git_metadata
        return {"status": "passed" if not outside and identity_ok and metadata_ok else "failed", "changedPaths": changed,
                "outOfScopePaths": outside, "protectedChanges": protected_paths, "identityMatches": identity_ok,
                "gitMetadataMatches": metadata_ok}

    def _git_metadata(self, workspace, identity):
        paths = {"pointer": workspace / ".git", "head": Path(identity["gitDir"]) / "HEAD",
                 "index": Path(identity["gitDir"]) / "index", "worktreeConfig": Path(identity["gitDir"]) / "config.worktree",
                 "commonConfig": Path(identity["commonDir"]) / "config"}
        return {key: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for key, path in paths.items()}

    def _worker(self, task, state, context, lock):
        workspace, identity, config, redactor, model = context
        directory = self._directory(task["taskId"])
        sequence = 0
        artifacts = []
        agent_result = {"status": "not_run", "steps": 0}
        review = {"status": "unavailable", "changedPaths": [], "outOfScopePaths": [], "protectedChanges": []}
        checks = {"status": "not_run", "targets": task["contract"]["checkTargets"], "checks": [], "reason": "EXECUTION_ERROR", "counts": None}
        incomplete = []
        stop = "execution_error"
        patch_export = {"status": "unavailable", "sha256": None, "bytes": None, "reason": "DIFF_UNAVAILABLE"}
        baseline, tools = None, None

        def save_state(**updates):
            state.update(updates, updatedAt=now())
            write_json(self._path(directory / "state.json"), state)

        def event(value):
            nonlocal sequence
            sequence += 1
            events = self._path(directory / "events")
            events.mkdir(exist_ok=True)
            write_json(self._path(events / f"{sequence:08d}.json"), {"seq": sequence, "at": now(), **value})
            if "step" in value:
                save_state(steps=value["step"])

        def artifact(identifier, kind, raw):
            target = self._path(directory / "public")
            target.mkdir(exist_ok=True)
            maximum = 2 * 1024 * 1024
            data = redactor.public(raw[:maximum].decode("utf-8", errors="replace"))
            self._path(target / (identifier + ".txt")).write_text(data, encoding="utf-8")
            artifacts.append({"artifactId": identifier, "kind": kind, "truncated": len(raw) > maximum})

        try:
            event({"type": "phase", "phase": "agent"})
            if self.preparation._identity(Path(identity["source"]), workspace) != identity:
                raise ValueError("Workspace identity changed")
            if git(workspace, "status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=all"):
                raise ValueError("Workspace became dirty")
            baseline = self._snapshot(workspace)
            if "LINK" in baseline.values():
                raise ValueError("Linked files unsupported for execution")
            source_snapshot = archive_agent_source(directory / "agent-source")
            if source_snapshot["status"] != "captured":
                raise ValueError("Agent source snapshot unavailable or changed")
            test_env = {**config["test_env"], "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider",
                        "PYTHONPATH": str(workspace), "PYTHONNOUSERSITE": "1"}
            tools = TaskRepoTools(workspace, contract=task["contract"], test_python=config["test_python"],
                                 test_env=test_env, test_timeout=config["test_timeout"],
                                 test_log_dir=directory / "tests", test_output_filter=redactor.output, test_junit=True)
            tools.git_metadata = self._git_metadata(workspace, identity)
            write_json(directory / "runtime.json", {"identity": identity, "testPython": str(config["test_python"]),
                       "testEnv": test_env, "testTimeout": config["test_timeout"], "source": self.source,
                       "baseCommit": task["contract"]["baseCommit"], "allowedPaths": task["contract"]["allowedPaths"]})
            tools.policy_check = lambda: self._review(workspace, identity, baseline, tools)["status"] == "passed"
            model = model if model is not None else self.model_factory()
            # Model identity is local evidence; never persist credentials or URLs.
            write_json(directory / "model.json", {"source": self.source, "model": getattr(model, "model", type(model).__name__),
                       "runtime": model.get_run_metadata() if hasattr(model, "get_run_metadata") else {}})
            if getattr(model, "api_key", None):
                redactor.secrets.add(model.api_key)
            prompt = json.dumps(task["contract"], ensure_ascii=False) + "\nOnly edit allowedPaths business code. Never edit tests/configuration. Only run frozen checkTargets."
            agent = RecordedAgent(model, tools, trace_dir=directory / "trace", max_steps=state["maxSteps"],
                                  recorder=event, redactor=redactor)
            agent_result = agent.run(prompt)
            write_json(directory / "agent-result.json", json.loads(redactor.private(json.dumps(agent_result, ensure_ascii=False))))
            stop = {"agent_reported_finished": "agent_reported_finished", "step_limit": "step_limit"}.get(agent_result["status"], "agent_error")
            save_state(status="finalizing", phase="final_checks", activity="fixed_checks")
            event({"type": "phase", "phase": "final_checks"})
            review = self._review(workspace, identity, baseline, tools)
            if review["status"] != "passed":
                stop = "policy_violation"
                checks["reason"] = "POLICY_VIOLATION"
                incomplete.append("工作副本身份或修改范围审查失败，最终检查未执行")
            elif not task["contract"]["allowTests"]:
                checks.update(status="skipped", reason="TESTS_NOT_AUTHORIZED")
                incomplete.append("用户未授权测试，未验证修复效果")
            else:
                checks.update(status="passed", reason=None)
                for index, target in enumerate(task["contract"]["checkTargets"], 1):
                    outcome = tools.run_tests(target)
                    log = outcome.get("test_log", {})
                    item = {"target": target, "status": outcome["check_status"], "counts": outcome["counts"],
                            "exitCode": outcome.get("exit_code"), "timedOut": outcome.get("timed_out", False),
                            "stdoutArtifactId": None, "stderrArtifactId": None}
                    if log.get("status") == "recorded":
                        for stream in ("stdout", "stderr"):
                            identifier = f"check-{index}-{stream}"
                            artifact(identifier, "test_" + stream, Path(log[stream]["path"]).read_bytes())
                            item[stream + "ArtifactId"] = identifier
                    if outcome.get("check_reason"):
                        item["reason"] = outcome["check_reason"]
                    checks["checks"].append(item)
                    if item["status"] == "failed":
                        checks["status"] = "failed"
                    review = self._review(workspace, identity, baseline, tools)
                    if review["status"] != "passed":
                        stop = "policy_violation"
                        incomplete.append("检查产生越界/受保护文件变动，后续检查停止")
                        for pending in task["contract"]["checkTargets"][index:]:
                            checks["checks"].append({"target": pending, "status": "not_run", "exitCode": None,
                                                     "timedOut": False, "reason": "POLICY_VIOLATION", "counts": None,
                                                     "stdoutArtifactId": None, "stderrArtifactId": None})
                        break
                actual = [c for c in checks["checks"] if c["status"] != "not_run"]
                if actual and all(c["counts"] is not None for c in actual):
                    checks["counts"] = {key: sum(c["counts"][key] for c in actual) for key in actual[0]["counts"]}
                if actual and all(c["status"] == "skipped" for c in actual):
                    checks.update(status="skipped", reason="ALL_TESTS_SKIPPED")
                if any(c["status"] == "not_run" for c in checks["checks"]):
                    checks.update(status="failed", reason="POLICY_VIOLATION")
                if checks["status"] != "passed":
                    incomplete.append("固定最终检查未全部通过")
        except Exception as exc:
            stop = "execution_error"
            incomplete.append("执行异常，保留已产生材料；结果不代表修复通过")
            try:
                write_json(directory / "failure.json", {"errorType": type(exc).__name__, "at": now()})
                event({"type": "error"})
            except Exception:
                pass
        finally:
            try:
                for pending in task["contract"]["checkTargets"][len(checks["checks"]):]:
                    checks["checks"].append({"target": pending, "status": "not_run", "exitCode": None, "timedOut": False,
                                             "counts": None, "reason": checks.get("reason") or "EXECUTION_ERROR",
                                             "stdoutArtifactId": None, "stderrArtifactId": None})
                if checks["status"] == "passed" and any(c["status"] == "not_run" for c in checks["checks"]):
                    checks.update(status="failed", reason="INCOMPLETE_FINAL_CHECKS")
                actual = [c for c in checks["checks"] if c["status"] != "not_run"]
                if actual and all(c["counts"] is not None for c in actual):
                    checks["counts"] = {key: sum(c["counts"][key] for c in actual) for key in actual[0]["counts"]}
                save_state(status="finalizing", phase="delivery", activity="saving_materials")
                if baseline is not None and tools is not None:
                    try:
                        review = self._review(workspace, identity, baseline, tools)
                        if review["status"] != "passed":
                            stop = "policy_violation"
                    except Exception:
                        review["status"] = "unavailable"
                        incomplete.append("最终身份或范围审查不可用")
                try:
                    if review["status"] == "unavailable":
                        raise ValueError("Scope evidence unavailable for diff extraction")
                    safe_paths = [p for p in review["changedPaths"] if _relative_path(p) and Path(p).name.lower() not in {"id_rsa", "id_ed25519"}]
                    omitted = sorted(set(review["changedPaths"]) - set(safe_paths))
                    patch = (git(workspace, "diff", "--binary", "--no-ext-diff", "--no-textconv", task["contract"]["baseCommit"], "--", *safe_paths)
                             if safe_paths else b"")
                    # Include newly created business files; protected files remain on disk,
                    # their hashes/paths are in review rather than copied into public logs.
                    if tools is not None:
                        tracked = set(git(workspace, "ls-files", "-z").decode("utf-8").split("\0"))
                        for relative in sorted(set(safe_paths) - tracked):
                            path = workspace / relative
                            if tools.editable(relative) and path.is_file() and path.stat().st_size <= 800000:
                                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                                patch += "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                                                for line in difflib.unified_diff([], lines, fromfile="/dev/null", tofile="b/" + relative)).encode("utf-8")
                            else:
                                omitted.append(relative)
                    if any(secret.encode("utf-8") in patch for secret in redactor.secrets):
                        patch_export = {"status": "not_exported", "sha256": None, "bytes": None, "reason": "KNOWN_SECRET_DETECTED"}
                        incomplete.append("补丁含已知密钥，拒绝导出原始补丁；副本保留待审查，网页 diff 已脱敏")
                    elif omitted:
                        patch_export = {"status": "not_exported", "sha256": None, "bytes": None, "reason": "INCOMPLETE_DIFF"}
                        incomplete.append("部分变动无法安全完整导出，原始补丁未保存；网页 diff 不覆盖全部材料")
                    else:
                        (directory / "candidate.patch").write_bytes(patch)
                        patch_export = {"status": "saved", "sha256": hashlib.sha256(patch).hexdigest(), "bytes": len(patch), "reason": None}
                    write_json(directory / "patch-export.json", patch_export)
                    artifact("diff", "diff", patch)
                except Exception:
                    incomplete.append("diff 未能完整保存")
                if stop != "agent_reported_finished":
                    incomplete.append("Agent 未以正常 finish 完成，停止原因见 stopReason")
                event({"type": "terminated"})
                event_files = sorted((directory / "events").glob("*.json"))
                artifact("events", "events", b"\n".join(p.read_bytes() for p in event_files))
                public_agent = {"status": agent_result["status"], "steps": agent_result.get("steps", 0),
                                "summary": redactor.public(agent_result.get("answer", ""))}
                if agent_result.get("reason"):
                    public_agent["error"] = redactor.public(agent_result["reason"])[:400]
                result = {"schemaVersion": 1, "taskId": task["taskId"], "executionId": state["executionId"],
                          "source": self.source, "stopReason": stop, "agent": public_agent,
                          "finalChecks": checks, "review": review, "artifacts": artifacts,
                          "patchExport": patch_export, "incomplete": incomplete, "independentlyVerified": False}
                write_json(self._path(directory / "result.json"), result)
                save_state(status="terminated", phase="delivery", activity="stopped", liveness="stopped",
                           stopReason=stop, finishedAt=now(), resultAvailable=True)
            except Exception:
                # A partial claim remains permanently consumed; GET conservatively
                # reports unknown after the OS lock is released. Never run again.
                pass
            finally:
                lock.__exit__(None, None, None)

    def events(self, task_id, after=0, limit=50):
        self.store.get(task_id)
        with self._io():
            directory = self._path(self._directory(task_id) / "events")
            rows = []
            for path in sorted(directory.glob("*.json")):
                if not re.fullmatch(r"[0-9]{8}\.json", path.name):
                    continue
                number = int(path.stem)
                if number > after:
                    row = read_json(self._path(path))
                    if row.get("seq") != number or set(row) - {"seq", "at", "type", "phase", "step", "action", "ok", "attempt", "outcome", "feedback"}:
                        raise ValueError("Invalid event")
                    rows.append(row)
                    if len(rows) > limit:
                        break
            return {"taskId": task_id, "events": rows[:limit], "nextCursor": rows[min(len(rows), limit)-1]["seq"] if rows else after,
                    "hasMore": len(rows) > limit}

    def result(self, task_id):
        self.store.get(task_id)
        with self._io():
            path = self._path(self._directory(task_id) / "result.json")
            if not path.is_file():
                raise TaskError(409, "RESULT_NOT_AVAILABLE", "完整结果尚未保存")
            result = read_json(path)
            if type(result) is not dict or result.get("taskId") != task_id or result.get("schemaVersion") != 1 or result.get("independentlyVerified") is not False:
                raise ValueError("Invalid result")
            return result

    def artifact(self, task_id, identifier, offset=0, limit=16000):
        result = self.result(task_id)
        entry = next((a for a in result["artifacts"] if a["artifactId"] == identifier), None)
        if entry is None or not re.fullmatch(r"diff|events|check-[1-9][0-9]*-(stdout|stderr)", identifier):
            raise TaskError(404, "ARTIFACT_NOT_FOUND", "材料不存在")
        with self._io():
            path = self._path(self._directory(task_id) / "public" / (identifier + ".txt"))
            content = path.read_text(encoding="utf-8")
            end = min(offset + limit, len(content))
            return {"artifactId": identifier, "kind": entry["kind"], "content": content[offset:end], "offset": offset,
                    "nextOffset": max(offset, end), "totalChars": len(content), "hasMore": end < len(content),
                    "redacted": True, "truncated": entry["truncated"]}
