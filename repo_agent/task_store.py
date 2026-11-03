"""Append-only ordinary task creation; records live outside public source."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid
from datetime import datetime

from .task_contract import TASK_ID, TaskError, load_json, validate_record


class TaskStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def directory(self, task_id):
        return self.root / f"task_{task_id}"

    def create(self, build_record) -> dict:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for _ in range(10):
                task_id = uuid.uuid4().hex
                directory = self.root / f"task_{task_id}"
                try:
                    directory.mkdir()
                except FileExistsError:
                    continue
                record = build_record(task_id)
                validate_record(record, task_id)
                temporary = directory / "task.json.tmp"
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(directory / "task.json")
                return record
            raise OSError("Task ID allocation exhausted")
        except OSError as exc:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "任务保存失败，请检查本机存储") from exc

    def get(self, task_id: str) -> dict:
        if not TASK_ID.fullmatch(task_id):
            raise TaskError(400, "INVALID_TASK_ID", "任务编号格式不正确")
        directory = self.directory(task_id)
        path = directory / "task.json"
        try:
            # resolve() catches junctions as well as ordinary symlinks on Windows.
            if directory.resolve() != directory or path.resolve() != path:
                raise TaskError(503, "STORAGE_UNAVAILABLE", "任务记录位置不符合要求")
            with path.open("rb") as stream:
                data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Oversized record")
            record = load_json(data)
            validate_record(record, task_id)
            return record
        except FileNotFoundError as exc:
            raise TaskError(404, "TASK_NOT_FOUND", "任务不存在") from exc
        except OSError as exc:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "无法读取本机任务记录") from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise TaskError(500, "TASK_RECORD_CORRUPT", "任务记录损坏，无法读取") from exc

    def latest(self) -> list[dict]:
        try:
            if not self.root.exists():
                return []
            records = []
            for directory in self.root.iterdir():
                task_id = directory.name.removeprefix("task_")
                if (directory.name.startswith("task_") and TASK_ID.fullmatch(task_id)
                        and (directory / "task.json").exists()):
                    records.append(self.get(task_id))
            records.sort(key=lambda r: (datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")), r["taskId"]), reverse=True)
            return records[:50]
        except OSError as exc:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "无法读取任务列表") from exc
