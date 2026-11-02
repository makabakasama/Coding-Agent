"""Read-only views of registered experiment rounds; never calls a runner."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .path_migrations import recorded_path
from .round_layout import ID_PATTERN
from .task_contract import TaskError, load_json
from .task_execution import Redactor


REGISTRY = {
    "browser-use": ("browser-use-5998", "5998", "examples.browser_use_5998"),
    "pydantic-ai": ("pydantic-ai-10525", "10525", "examples.pydantic_ai_10525"),
}


class TaskHistory:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _registration(self, project, task=None):
        if project not in REGISTRY or task is not None and task != REGISTRY[project][0]:
            raise TaskError(404, "HISTORY_NOT_FOUND", "未登记该实验历史")
        # The factories only construct existing local Spec values. No runner is invoked.
        from importlib import import_module
        spec = import_module(REGISTRY[project][2]).experiment_spec()
        return REGISTRY[project], spec, self.root / "runs" / spec.name

    def projects(self, configured):
        result = [{**p, "modes": ["ordinary"], "historyAvailability": "not_applicable",
                   "canCreateTask": True, "readOnly": False} for p in configured]
        for project, (task, _, _) in REGISTRY.items():
            item = next((p for p in result if p["projectId"] == project), None)
            history = "available" if (self.root / "runs" / task).is_dir() else "missing"
            if item:
                item.update(modes=["ordinary", "experiment"], historyAvailability=history)
            else:
                result.append({"projectId": project, "name": project, "modes": ["experiment"],
                               "historyAvailability": history, "canCreateTask": False, "readOnly": True})
        return result

    def _safe(self, path, directory):
        path = Path(path)
        if path.resolve() != path or not path.is_relative_to(directory):
            raise TaskError(503, "STORAGE_UNAVAILABLE", "历史材料位置不符合登记范围")
        return path

    def _read(self, path, directory, warnings):
        try:
            path = self._safe(path, directory)
            if not path.is_file():
                warnings.append("未提供 " + path.relative_to(directory).as_posix())
                return {}
            with path.open("rb") as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Report too large")
            value = load_json(raw)
            if type(value) is not dict:
                raise ValueError("Not an object")
            return value
        except (OSError, ValueError, TaskError, RecursionError):
            warnings.append("无法读取 " + Path(path).name)
            return {}

    def _rounds(self, project, task):
        _, spec, parent = self._registration(project, task)
        warnings, records = [], []
        if not parent.is_dir():
            return [], ["历史根目录缺失"]
        for directory in sorted(parent.iterdir(), reverse=True):
            if not re.fullmatch(r"(?:run|prepare|legacy)_\d{8}-\d{6}_[a-f0-9]{8}", directory.name):
                continue
            meta = self._read(directory / "round.json", parent, warnings)
            identifier, entry = meta.get("round_id"), meta.get("entry")
            if (not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier)
                    or entry not in ("run", "prepare", "legacy") or meta.get("experiment") != spec.name
                    or directory.name != entry + "_" + identifier):
                warnings.append("轮次元数据缺失或不匹配：" + directory.name)
                continue
            records.append((directory, meta))
        return records, warnings

    def task(self, project, task):
        registration, _, parent = self._registration(project, task)
        records, warnings = self._rounds(project, task)
        return {"projectId": project, "taskKey": task, "mode": "experiment", "issueId": registration[1],
                "title": project + " #" + registration[1], "requirementRef": None, "requirementRevision": None,
                "roundCount": len(records), "historyAvailability": "available" if parent.is_dir() else "missing",
                "readOnly": True, "warnings": warnings, "legacyLinks": []}

    def tasks(self, project):
        registration, _, _ = self._registration(project)
        return {"tasks": [self.task(project, registration[0])]}

    def rounds(self, project, task):
        records, warnings = self._rounds(project, task)
        return {"rounds": [self._view(project, task, directory, meta)[0] for directory, meta in records], "warnings": warnings}

    def _find(self, project, task, identifier):
        records, _ = self._rounds(project, task)
        for directory, meta in records:
            if meta["round_id"] == identifier:
                return directory, meta
        raise TaskError(404, "HISTORY_NOT_FOUND", "未找到登记轮次")

    def round(self, project, task, identifier):
        directory, meta = self._find(project, task, identifier)
        return self._view(project, task, directory, meta)[0]

    def _view(self, project, task, directory, meta):
        warnings, files, artifacts = [], {}, []
        _, spec, _ = self._registration(project, task)

        def register(identifier, kind, label, path):
            try:
                path = self._safe(path, directory)
                available = path.is_file()
            except (OSError, TaskError):
                available = False
            artifacts.append({"artifactId": identifier, "kind": kind, "label": label, "available": available})
            if available:
                files[identifier] = path
                return identifier
            return None

        def logs(report, report_path, prefix):
            rows = report.get("tests", [])
            if not isinstance(rows, list):
                return
            for index, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    continue
                for stream in ("stdout", "stderr"):
                    value = row.get(stream)
                    if isinstance(value, dict):
                        value = value.get("path")
                    if not isinstance(value, str):
                        continue
                    try:
                        path = Path(value)
                        path = recorded_path(path, spec.path_migrations) if path.is_absolute() else report_path.parent / path
                        register(f"{prefix}-test-{index}-{stream}", "test_" + stream, f"{prefix} 检查{index} {stream}", path)
                    except (ValueError, OSError):
                        warnings.append("无法解析检查输出引用")

        fixed = [("round", "report", "轮次元数据", "round.json"), ("summary", "summary", "历史摘要", "summary.md"),
                 ("experiment", "report", "原总报告", "experiment.json"), ("preparation", "report", "准备报告", "repair/manifest.json"),
                 ("agent", "report", "Agent报告", "repair/agent-run.json"), ("candidate", "report", "候选导出报告", "repair/artifacts/report.json"),
                 ("regression-patch", "patch", "冻结R补丁", "repair/regression.patch"), ("agent-patch", "patch", "候选P补丁", "repair/artifacts/agent.patch")]
        refs = {identifier: register(identifier, kind, label, directory / relative) for identifier, kind, label, relative in fixed}
        total = self._read(directory / "experiment.json", directory, warnings)
        baseline = self._read(directory / "repair/manifest.json", directory, warnings)
        agent = self._read(directory / "repair/agent-run.json", directory, warnings)
        candidate = self._read(directory / "repair/artifacts/report.json", directory, warnings)
        logs(baseline, directory / "repair/manifest.json", "preparation")
        verifications = []
        for child in sorted(directory.glob("verify-*")):
            if not re.fullmatch(r"verify-\d{2,}", child.name):
                continue
            modern = (child / "verification.json").is_file()
            path = child / ("verification.json" if modern else "manifest.json")
            if not path.is_file():
                continue
            report = self._read(path, directory, warnings)
            report_ref = register(child.name + "-report", "report", child.name + " 验收报告", path)
            confirmed = None
            verification_warnings = []
            if modern:
                if report.get("independently_verified") is False:
                    confirmed = False
                elif report.get("independently_verified") is True and report.get("status") == "independently_verified":
                    confirmed = True
                elif report.get("independently_verified") is True:
                    verification_warnings.append("验收结论字段不一致")
            else:
                verification_warnings.append("旧格式报告，未升级为新协议独立验收结论")
            tests = report.get("tests", [])
            tests = tests if isinstance(tests, list) else []
            verifications.append({"verificationId": child.name, "sourceFormat": "verification_v1" if modern else "legacy_verification",
                                  "createdAt": report.get("started_at_utc"), "rawStatus": report.get("status"),
                                  "independentlyVerified": confirmed, "reportRef": report_ref,
                                  "candidateSha256": report.get("patch_sha256", report.get("agent_patch_sha256")),
                                  "integrityStatus": "not_checked", "warnings": verification_warnings,
                                  "tests": [{"target": t.get("target"), "exitCode": t.get("exit_code"), "timedOut": t.get("timed_out"),
                                             "counts": t.get("counts"), "matchesExpectation": t.get("matches_expectation")} for t in tests if isinstance(t, dict)]})
            logs(report, path, child.name)
            if modern and (child / "manifest.json").is_file():
                register(child.name + "-legacy-report", "report", child.name + " 旧格式报告", child / "manifest.json")
        redactor = Redactor()
        notes = meta.get("notes", [])
        warnings.extend(redactor.public(str(n)) for n in (notes if isinstance(notes, list) else []))
        linked = bool(total or baseline or agent or candidate or verifications)
        if not linked:
            warnings.append("未关联历史材料：没有可确认需求归属的运行证据，taskKey未提供")
        patch = candidate.get("patch")
        result = {"projectId": project, "taskKey": task if linked else None, "roundId": meta["round_id"], "entry": meta["entry"],
                  "mode": "experiment", "sourceFormat": "legacy_round" if meta["entry"] == "legacy" else "experiment_round",
                  "createdAt": total.get("started_at_utc", baseline.get("started_at_utc")),
                  "originalRunConclusion": total.get("status"), "preparationStatus": baseline.get("status"),
                  "agentStatus": agent.get("status"), "exportStatus": candidate.get("status"),
                  "verificationCount": len(verifications), "evidenceState": "unlinked" if not linked else "partial" if warnings else "available",
                  "association": "recorded" if linked else "unlinked", "requirementSnapshotRef": None,
                  "baseCommit": baseline.get("base_commit"), "regressionSha256": baseline.get("regression_patch_sha256"),
                  "candidateSha256": patch.get("sha256") if isinstance(patch, dict) else None, "liveness": "not_assessed", "integrityStatus": "not_checked",
                  "readOnly": True, "warnings": warnings, "roundReportRef": refs["round"], "originalRunReportRef": refs["experiment"],
                  "preparationReportRef": refs["preparation"], "agentReportRef": refs["agent"], "candidateReportRef": refs["candidate"],
                  "artifacts": artifacts, "verifications": verifications}
        return result, files

    def artifact(self, project, task, identifier, artifact_id, offset=0, limit=16000):
        directory, meta = self._find(project, task, identifier)
        view, files = self._view(project, task, directory, meta)
        if artifact_id not in files:
            raise TaskError(404, "ARTIFACT_NOT_FOUND", "历史材料未提供")
        path = files[artifact_id]
        try:
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
        except OSError as exc:
            raise TaskError(503, "STORAGE_UNAVAILABLE", "历史材料无法读取") from exc
        truncated = len(raw) > 2 * 1024 * 1024
        raw = raw[:2 * 1024 * 1024]
        redactor = Redactor()

        def sanitized(value):
            if isinstance(value, dict):
                return {k: "[LOCAL_CONFIGURATION]" if k in ("environment", "test_environment", "test_env", "test_python", "python", "source", "workspace")
                        else "[REDACTED]" if re.search(r"api_key|password|secret|credential|access_token", k, re.I)
                        else sanitized(v) for k, v in value.items()}
            if isinstance(value, list):
                return [sanitized(v) for v in value]
            return redactor.public(value) if isinstance(value, str) else value

        if path.suffix == ".json":
            if truncated:
                content = "报告超过公开上限，未公开可能包含本机配置的JSON片段，请在本机查看。"
            else:
                try:
                    content = json.dumps(sanitized(load_json(raw)), ensure_ascii=False, indent=2)
                except (ValueError, UnicodeError):
                    content = "报告无法解析，未公开原始片段。"
        else:
            content = redactor.public(raw.decode("utf-8", errors="replace"))
        kind = next(a["kind"] for a in view["artifacts"] if a["artifactId"] == artifact_id)
        end = min(offset + limit, len(content))
        return {"artifactId": artifact_id, "kind": kind, "content": content[offset:end], "offset": offset,
                "nextOffset": max(offset, end), "totalChars": len(content), "hasMore": end < len(content),
                "redacted": True, "truncated": truncated}
