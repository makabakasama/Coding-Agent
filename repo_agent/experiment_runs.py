"""Start registered known-answer experiments from the local web workbench."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
import threading

from .task_contract import TaskError
from .task_preparation import exclusive


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ExperimentRuns:
    """One registered experiment at a time, reusing the existing Runner pipeline."""

    def __init__(self, root: Path, store, history, *, runner_factory=None):
        self.root = Path(root).resolve()
        self.store = store
        self.history = history
        self.runner_factory = runner_factory
        self._state_lock = threading.Lock()
        self._active = None
        self.threads = []

    def _lock_path(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        return self.store.root / "execution-global.lock"

    @staticmethod
    def _config_available(spec):
        return all(path is None or Path(path).exists()
                   for path in (spec.source, spec.test_python, spec.regression_patch))

    def status(self, project, task):
        _, spec, _ = self.history._registration(project, task)
        with self._state_lock:
            latest = None if self._active is None else dict(self._active)
        busy = latest is not None and latest["status"] in ("starting", "running")
        run = latest if latest is not None and (
            latest["projectId"], latest["taskKey"]
        ) == (project, task) else None
        active = run is not None and run["status"] in ("starting", "running")
        configured = self._config_available(spec)
        with exclusive(self._lock_path()) as available:
            can_start = configured and available and not busy
        return {"run": run, "active": active, "canStart": can_start,
                "startBlockReason": None if can_start else (
                    "EXPERIMENT_CONFIG_UNAVAILABLE" if not configured else "EXPERIMENT_BUSY"
                ),
                "liveness": "confirmed" if active else "not_assessed"}

    def start(self, project, task, value):
        if type(value) is not dict or value:
            raise TaskError(400, "VALIDATION_ERROR", "实验启动请求须为空 JSON 对象")
        registration, spec, _ = self.history._registration(project, task)
        if not self._config_available(spec):
            raise TaskError(409, "EXPERIMENT_CONFIG_UNAVAILABLE",
                            "登记的实验源码、环境或冻结测试补丁不可用")

        lock = exclusive(self._lock_path())
        acquired = lock.__enter__()
        handed_over = False
        allocated = threading.Event()
        try:
            if not acquired:
                raise TaskError(409, "EXPERIMENT_BUSY", "其他普通任务或实验正在执行")
            with self._state_lock:
                if self._active is not None and self._active["status"] in ("starting", "running"):
                    raise TaskError(409, "EXPERIMENT_BUSY", "已有实验正在执行")
                record = {
                    "projectId": project,
                    "taskKey": task,
                    "roundId": None,
                    "status": "starting",
                    "startedAt": _now(),
                    "finishedAt": None,
                    "conclusion": None,
                }
                self._active = record

            def allocated_callback(_directory, identifier):
                with self._state_lock:
                    record.update(roundId=identifier, status="running")
                allocated.set()

            thread = threading.Thread(
                target=self._worker,
                args=(registration[2], spec, record, allocated_callback, lock),
                daemon=True,
                name=f"experiment-{project}-{task}",
            )
            thread.start()
            handed_over = True
            self.threads.append(thread)
            if not allocated.wait(10):
                raise TaskError(503, "EXPERIMENT_NOT_ALLOCATED", "实验未能建立轮次记录")
            with self._state_lock:
                return 202, dict(self._active)
        finally:
            if not handed_over:
                with self._state_lock:
                    if self._active is not None and self._active["roundId"] is None:
                        self._active = None
                lock.__exit__(None, None, None)

    def _worker(self, module_name, spec, record, allocated_callback, lock):
        try:
            if self.runner_factory is None:
                result = import_module(module_name).run_experiment(spec, on_allocated=allocated_callback)
            else:
                result = self.runner_factory(spec).run_experiment(spec, on_allocated=allocated_callback)
            conclusion = result.get("status")
            status = ("completed" if conclusion == "independently_verified"
                      else "interrupted" if conclusion == "interrupted" else "failed")
        except BaseException as exc:
            conclusion = "exception"
            status = "failed"
            record["errorType"] = type(exc).__name__
        finally:
            with self._state_lock:
                record.update(status=status, conclusion=conclusion, finishedAt=_now())
            lock.__exit__(None, None, None)
