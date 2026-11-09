"""Ordinary task contracts. No HTTP, disk IO, or model calls."""
from __future__ import annotations

from datetime import datetime
import json
import re


TASK_ID = re.compile(r"[0-9a-f]{32}\Z")
PROJECT_ID = re.compile(r"[a-z0-9_-]{1,64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class TaskError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: list | None = None):
        super().__init__(message)
        self.status, self.code = status, code
        self.message, self.fields = message, fields or []

    def response(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "fields": self.fields}}


def load_json(data: str | bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _controls(value: str) -> bool:
    return any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value)


def _relative_path(value: str) -> bool:
    if (not value or value.startswith("-") or _controls(value)
            or any(c in value for c in '\\:*?"<>|')):
        return False
    parts = value.split("/")
    protected = {".git", ".venv", "venv", "node_modules"}
    return all(part and part not in (".", "..") and not part.endswith((" ", "."))
               and part.lower() not in protected and not part.lower().startswith(".env")
               for part in parts)


def validate_contract(value) -> dict:
    if not isinstance(value, dict):
        raise TaskError(400, "VALIDATION_ERROR", "任务必须是 JSON 对象")
    required = {"projectId", "revision", "current", "expected", "allowedPaths", "allowTests", "checkTargets"}
    allowed = required | {"kind", "maxSteps"}
    errors = []

    def error(field, message):
        errors.append({"field": field, "message": message})

    for key in sorted(required - value.keys()):
        error(key, "缺少必填字段")
    for key in sorted(value.keys() - allowed):
        error(key, "不支持的字段")
    result = {}
    for key, limit in (("projectId", 64), ("revision", 160), ("current", 4000), ("expected", 4000)):
        text = value.get(key)
        if type(text) is not str:
            error(key, "必须是字符串")
            continue
        text = text.strip()
        if not 1 <= len(text) <= limit or any(0xD800 <= ord(c) <= 0xDFFF for c in text):
            error(key, f"长度必须为 1–{limit} 且包含有效文字")
        if key == "projectId" and not PROJECT_ID.fullmatch(text):
            error(key, "项目编号格式不正确")
        if key == "revision" and (text.startswith("-") or _controls(text)):
            error(key, "版本引用格式不正确")
        result[key] = text
    result["kind"] = value.get("kind", "fix_bug")
    if result["kind"] not in ("read_code", "fix_bug", "add_feature"):
        error("kind", "须为 read_code、fix_bug 或 add_feature")
    result["maxSteps"] = value.get("maxSteps", 12)
    if type(result["maxSteps"]) is not int or not 1 <= result["maxSteps"] <= 50:
        error("maxSteps", "必须是 1–50 的整数")
    result["allowTests"] = value.get("allowTests")
    if type(result["allowTests"]) is not bool:
        error("allowTests", "必须是布尔值")
    for key, count, length in (("allowedPaths", 100, 500), ("checkTargets", 20, 220)):
        entries = value.get(key)
        result[key] = []
        if type(entries) is not list or len(entries) > count:
            error(key, f"必须是最多 {count} 项的数组")
            continue
        for entry in entries:
            if type(entry) is not str:
                error(key, "每一项必须是字符串")
                continue
            entry = entry.strip()
            valid = 1 <= len(entry) <= length
            if key == "allowedPaths":
                valid = valid and _relative_path(entry)
            else:
                path, *nodes = entry.split("::")
                valid = (valid and _relative_path(path)
                         and all(node and not _controls(node) for node in nodes))
            if not valid or entry in result[key]:
                error(key, "路径或检查目标无效，或出现重复项")
            result[key].append(entry)
    if result["kind"] == "read_code" and result["allowedPaths"]:
        error("allowedPaths", "阅读任务不允许修改，路径数组必须为空")
    elif result["kind"] != "read_code" and not result["allowedPaths"]:
        error("allowedPaths", "至少指定一个允许修改路径")
    if result["allowTests"] is True and not result["checkTargets"]:
        error("checkTargets", "允许运行测试时必须指定检查目标")
    if result["allowTests"] is False and result["checkTargets"]:
        error("checkTargets", "不允许测试时检查目标必须为空")
    if errors:
        raise TaskError(400, "VALIDATION_ERROR", "任务信息不符合要求", errors)
    return result


def validate_record(record, task_id: str) -> None:
    """Stored records must be canonical, not silently repaired on reads."""
    try:
        if (type(record) is not dict or set(record) != {
                "schemaVersion", "taskId", "recordType", "status", "createdAt", "contract"}
                or type(record["schemaVersion"]) is not int or record["schemaVersion"] != 1
                or record["taskId"] != task_id or not TASK_ID.fullmatch(task_id)
                or record["recordType"] != "actual" or record["status"] != "created"):
            raise ValueError("Invalid envelope")
        stamp = record["createdAt"]
        if type(stamp) is not str or not stamp.endswith("Z"):
            raise ValueError("Invalid timestamp")
        datetime.fromisoformat(stamp[:-1] + "+00:00")
        contract = record["contract"]
        if type(contract) is not dict or set(contract) != {
                "projectId", "requestedRevision", "baseCommit", "kind", "current", "expected",
                "allowedPaths", "allowTests", "checkTargets", "maxSteps"}:
            raise ValueError("Invalid contract")
        if type(contract["baseCommit"]) is not str or not COMMIT.fullmatch(contract["baseCommit"]):
            raise ValueError("Invalid commit")
        request = {k: v for k, v in contract.items() if k not in ("baseCommit", "requestedRevision")}
        request["revision"] = contract["requestedRevision"]
        if validate_contract(request) != request:
            raise ValueError("Noncanonical contract")
    except (KeyError, ValueError, TypeError, TaskError, OverflowError) as exc:
        raise TaskError(500, "TASK_RECORD_CORRUPT", "任务记录损坏，无法读取") from exc
