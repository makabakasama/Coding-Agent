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
from .experiment_runs import ExperimentRuns


class TaskService:
    def __init__(self, store: TaskStore, projects: TaskProjects, *, workspace_root=None,
                 model_factory=None, project_root=None, experiment_runner_factory=None):
        self.store, self.projects = store, projects
        self.preparation = TaskPreparation(store, projects, workspace_root or Path(__file__).resolve().parent.parent / "experiments/tasks")
        self.execution = TaskExecution(store, projects, self.preparation, model_factory=model_factory)
        root = Path(project_root or Path(__file__).resolve().parent.parent).resolve()
        self.history = TaskHistory(root)
        self.experiments = ExperimentRuns(root, store, self.history, runner_factory=experiment_runner_factory)

    def prepare(self, task_id, value):
        if type(value) is not dict or value:
            raise TaskError(400, "VALIDATION_ERROR", "准备请求须为空 JSON 对象")
        return self.preparation.query(task_id, prepare=True)

    def create(self, value, *, requirement=None) -> dict:
        contract = validate_contract(value)
        commit = self.projects.resolve(contract["projectId"], contract["revision"])
        contract["requestedRevision"] = contract.pop("revision")
        contract["baseCommit"] = commit

        def build(task_id):
            return {"schemaVersion": 1, "taskId": task_id, "recordType": "actual",
                    "status": "created", "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "contract": contract}

        return self.store.create(build, requirement=requirement) if requirement is not None else self.store.create(build)

    def ordinary_tasks(self, project):
        if project not in {p["projectId"] for p in self.projects.list()}:
            raise TaskError(404, "TASK_NOT_FOUND", "项目不存在")
        values = self.store.requirements(project) if hasattr(self.store, "requirements") else []
        return {"tasks": [{**v, "roundCount": len(self.store.rounds(project, v["taskKey"]))} for v in values]}

    def ordinary_task(self, project, key):
        if not hasattr(self.store, "requirement"):
            raise TaskError(404, "TASK_NOT_FOUND", "需求不存在")
        _, value = self.store.requirement(project, key)
        return value

    def new_round(self, project, key, value):
        if type(value) is not dict or value:
            raise TaskError(400, "VALIDATION_ERROR", "新轮次请求须为空 JSON 对象")
        contract = dict(self.ordinary_task(project, key)["contract"])
        contract["revision"] = contract.pop("requestedRevision")
        contract.pop("baseCommit")
        return self.create(contract, requirement=(project, key))

    def organization(self, task_id):
        self.get(task_id)
        return self.store.round_info(task_id) if hasattr(self.store, "round_info") else None

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
                result_available = any(self.execution._path(self.execution._directory(task_id) / name).is_file()
                                       for name in ('result.json', 'termination.json'))
            except (TaskError, OSError):
                result_available = False
            tasks.append({"taskId": task_id, "createdAt": record["createdAt"],
                          "projectId": record["contract"]["projectId"], "current": record["contract"]["current"],
                          "executionStatus": status, "resultAvailable": result_available})
        return tasks
