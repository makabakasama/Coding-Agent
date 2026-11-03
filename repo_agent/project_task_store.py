"""Ordinary requirements and append-only rounds in the existing project layout."""
from __future__ import annotations

from pathlib import Path
import uuid

from .round_layout import allocate_round
from .task_contract import PROJECT_ID, TASK_ID, TaskError, validate_record
from .task_preparation import read_json, write_json
from .task_store import TaskStore


class ProjectTaskStore(TaskStore):
    def __init__(self, legacy_root, runs_root, workspace_root):
        super().__init__(Path(legacy_root))
        self.runs_root = Path(runs_root).resolve()
        self.project_workspaces = Path(workspace_root).resolve()
        self._locations = {}

    def _requirements(self):
        if not self.runs_root.exists():
            return
        for path in self.runs_root.glob("*/task.json"):
            if path.resolve() != path:
                raise TaskError(503, "STORAGE_UNAVAILABLE", "需求位置不符合要求")
            value = read_json(path)
            if value.get("sourceFormat") == "ordinary_v2":
                if (value.get("taskKey") != path.parent.name
                        or not PROJECT_ID.fullmatch(value.get("projectId", ""))):
                    raise TaskError(500, "TASK_RECORD_CORRUPT", "需求记录损坏")
                yield path.parent, value

    def requirement(self, project, key):
        for directory, value in self._requirements():
            if value["projectId"] == project and value["taskKey"] == key:
                return directory, value
        raise TaskError(404, "TASK_NOT_FOUND", "需求不存在")

    def directory(self, task_id):
        if not TASK_ID.fullmatch(task_id):
            raise TaskError(400, "INVALID_TASK_ID", "任务编号格式不正确")
        legacy = super().directory(task_id)
        if legacy.exists():
            return legacy
        if task_id in self._locations:
            return self._locations[task_id]
        for parent, _ in self._requirements():
            for path in parent.glob("run_*/round.json"):
                value = read_json(path)
                if value.get("runTaskId") == task_id:
                    self._locations[task_id] = path.parent
                    return path.parent
        return legacy

    def round_info(self, task_id):
        path = self.directory(task_id) / "round.json"
        return read_json(path) if path.exists() else None

    def create(self, build_record, *, requirement=None):
        try:
            task_id = uuid.uuid4().hex
            record = build_record(task_id)
            validate_record(record, task_id)
            contract = record["contract"]
            self.root.mkdir(parents=True, exist_ok=True)  # Shared locks / legacy config only.
            self.runs_root.mkdir(parents=True, exist_ok=True)
            if requirement is None:
                key = contract["projectId"] + "-" + uuid.uuid4().hex[:12]
                parent = self.runs_root / key
                parent.mkdir()
                value = {"sourceFormat": "ordinary_v2", "mode": "ordinary",
                         "projectId": contract["projectId"], "taskKey": key,
                         "createdAt": record["createdAt"], "title": contract["current"],
                         "contract": contract}
                write_json(parent / "task.json", value)
            else:
                parent, value = self.requirement(*requirement)
            directory, identifier = allocate_round(parent, "run", value["taskKey"])
            metadata = read_json(directory / "round.json")
            metadata.update(mode="ordinary", projectId=value["projectId"],
                            taskKey=value["taskKey"], runTaskId=task_id,
                            sourceFormat="ordinary_v2")
            write_json(directory / "round.json", metadata)
            write_json(directory / "task.json", record)
            self._locations[task_id] = directory
            return record
        except OSError as exc:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "需求或轮次保存失败，保留已有材料") from exc

    def latest(self):
        records = super().latest()
        for parent, _ in self._requirements():
            for path in parent.glob("run_*/task.json"):
                value = read_json(path)
                records.append(self.get(value["taskId"]))
        records.sort(key=lambda r: (r["createdAt"], r["taskId"]), reverse=True)
        return records[:50]

    def requirements(self, project):
        return [value for _, value in self._requirements() if value["projectId"] == project]

    def rounds(self, project, key):
        parent, _ = self.requirement(project, key)
        result = []
        for path in parent.glob("run_*/task.json"):
            record = read_json(path)
            validate_record(record, record["taskId"])
            info = read_json(path.parent / "round.json")
            result.append({"roundId": info["round_id"], "runTaskId": record["taskId"],
                           "createdAt": record["createdAt"], "baseCommit": record["contract"]["baseCommit"],
                           "kind": record["contract"]["kind"], "mode": "ordinary"})
        return sorted(result, key=lambda r: r["roundId"], reverse=True)
