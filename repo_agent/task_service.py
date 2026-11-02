"""Shared create/query use cases. Creating a task never starts an Agent."""
from __future__ import annotations

from datetime import datetime, timezone

from .task_contract import validate_contract
from .task_projects import TaskProjects
from .task_store import TaskStore
from .task_preparation import TaskPreparation
from .task_contract import TaskError
from pathlib import Path
from .task_execution import TaskExecution
from .task_history import TaskHistory


class TaskService:
    def __init__(self, store: TaskStore, projects: TaskProjects, *, workspace_root=None, model_factory=None):
        self.store, self.projects = store, projects
        self.preparation = TaskPreparation(store, projects, workspace_root or Path(__file__).resolve().parent.parent / "experiments/tasks")
        self.execution = TaskExecution(store, projects, self.preparation, model_factory=model_factory)
        self.history = TaskHistory(Path(__file__).resolve().parent.parent)

    def prepare(self, task_id, value):
        if type(value) is not dict or value:
            raise TaskError(400, "VALIDATION_ERROR", "准备请求须为空 JSON 对象")
        return self.preparation.query(task_id, prepare=True)

    def create(self, value) -> dict:
        contract = validate_contract(value)
        commit = self.projects.resolve(contract["projectId"], contract["revision"])
        contract["requestedRevision"] = contract.pop("revision")
        contract["baseCommit"] = commit

        def build(task_id):
            return {"schemaVersion": 1, "taskId": task_id, "recordType": "actual",
                    "status": "created", "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "contract": contract}

        return self.store.create(build)

    def get(self, task_id: str) -> dict:
        return self.store.get(task_id)

    def list(self) -> list[dict]:
        tasks = []
        for record in self.store.latest():
            task_id = record["taskId"]
            try:
                status = self.execution.get(task_id, check_readiness=False)["status"]
            except TaskError:
                status = "unknown"
            try:
                result_available = self.execution._path(self.execution._directory(task_id) / "result.json").is_file()
            except (TaskError, OSError):
                result_available = False
            tasks.append({"taskId": task_id, "createdAt": record["createdAt"],
                          "projectId": record["contract"]["projectId"], "current": record["contract"]["current"],
                          "executionStatus": status, "resultAvailable": result_available})
        return tasks
