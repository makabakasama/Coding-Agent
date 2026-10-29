"""Allocate one human-readable round; summaries describe evidence, never invent it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import uuid

BEIJING = timezone(timedelta(hours=8))
ID_PATTERN = re.compile(r"\d{8}-\d{6}_[a-f0-9]{8}")


def new_round_id() -> str:
    return datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S") + "_" + uuid.uuid4().hex[:8]


def issue_key(name: str) -> str:
    match = re.search(r"-(\d+)$", name)
    return match[1] if match else name


def workspace_name(name: str, identifier: str, role: str) -> str:
    if not ID_PATTERN.fullmatch(identifier) or not re.fullmatch(r"agent|verify-\d{2,}|legacy-[a-z0-9-]+", role):
        raise ValueError("Invalid round identifier or workspace role")
    return issue_key(name) + "_" + identifier + "_" + role


def initialize_round(directory: Path, identifier: str, entry: str, name: str, **extra) -> None:
    data = {"round_id": identifier, "entry": entry, "experiment": name,
            "display_timezone": "UTC+08:00", **extra}
    with (directory / "round.json").open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)


def allocate_round(parent: Path, entry: str, name: str) -> tuple[Path, str]:
    if entry not in ("run", "prepare", "legacy"):
        raise ValueError("Invalid round entry type")
    parent.mkdir(parents=True, exist_ok=True)
    reservations = parent / ".round-ids"
    reservations.mkdir(exist_ok=True)
    for _ in range(20):
        identifier = new_round_id()
        directory = parent / (entry + "_" + identifier)
        try:
            if any((parent / (kind + "_" + identifier)).exists() for kind in ("run", "prepare", "legacy")):
                continue
            (reservations / identifier).mkdir()
            directory.mkdir()
        except FileExistsError:
            continue
        initialize_round(directory, identifier, entry, name)
        return directory, identifier
    raise FileExistsError("Could not allocate a unique round")


def find_round(path: Path) -> Path | None:
    for directory in (path.parent, *path.parents):
        if (directory / "round.json").is_file():
            return directory
    return None


def allocate_verification(directory: Path) -> tuple[Path, str]:
    for number in range(1, 10000):
        role = f"verify-{number:02d}"
        destination = directory / role
        try:
            destination.mkdir()
            return destination, role
        except FileExistsError:
            continue
    raise FileExistsError("No verification slot available")


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def write_summary(directory: Path, migration: Path | None = None) -> None:
    from .path_migrations import recorded_path
    metadata = _read(directory / "round.json")
    if not metadata:
        return
    baseline = _read(directory / "repair/manifest.json")
    receipt = _read(directory / "repair/agent-run.json")
    export = _read(directory / "repair/artifacts/report.json")
    total = _read(directory / "experiment.json")
    stage = total.get("stage", "Agent（独立验收见下）" if receipt else baseline.get("stage", "记录缺失"))
    status = total.get("status", receipt.get("status", baseline.get("status", "未知")))
    lines = ["# 实验轮次 " + metadata["round_id"], "",
             "- 入口类型：`" + metadata["entry"] + "`（命名不随成功或失败改变）。",
             "- 显示时间：北京时间 UTC+08:00；原始记录中的 UTC 时间保持原样。",
             "- 阶段/状态：`" + str(stage) + "` / `" + str(status) + "`。",
             "- Agent 状态：`" + str(receipt.get("status", "未运行或无记录")) + "`；finish 不等于独立验收。", ""]
    def location(value):
        if not value:
            return "未记录"
        return str(recorded_path(value, migration))
    lines += ["## G / R / P 与工作副本", "",
              "- G：`" + str(baseline.get("base_commit", baseline.get("requested_revision", "未记录"))) + "`。",
              "- R SHA256：`" + str(baseline.get("regression_patch_sha256") or "未记录") + "`；材料：`"
              + str(directory / "repair/regression.patch") + "`（以文件实际存在为准）。",
              "- P SHA256：`" + str(export.get("patch", {}).get("sha256", "未记录")) + "`；材料：`"
              + location(export.get("patch", {}).get("path")) + "`。",
              "- Agent 工作副本：`" + location(baseline.get("workspace", metadata.get("workspace"))) + "`。", "",
              "## 测试与独立验收", ""]
    if baseline.get("tests"):
        for test in baseline["tests"]:
            lines.append("- G+R `" + str(test.get("target")) + "`：退出码 `" + str(test.get("exit_code"))
                         + "`，计数 `" + str(test.get("counts", {})) + "`，符合预期 `" + str(test.get("matches_expectation")) + "`。")
    else:
        lines.append("- G+R：标准清单无测试结果列表；不能据此认定缺陷已复现，旧格式材料见 repair/。")
    verifications = sorted(directory.glob("verify-*/verification.json"))
    for path in verifications:
        verification = _read(path)
        confirmed = (verification.get("status") == "independently_verified"
                     and verification.get("independently_verified") is True)
        lines += ["- `" + path.parent.name + "`：`" + str(verification.get("status", "未知"))
                  + "`，独立验收通过 `" + str(confirmed) + "`；副本 `" + location(verification.get("workspace"))
                  + "`；报告 `" + str(path) + "`。"]
        if verification.get("error"):
            lines.append("- 验收失败原因：" + str(verification["error"]).replace("\n", " "))
        for test in verification.get("tests", []):
            lines.append("- 验收 `" + str(test.get("target")) + "`：退出码 `" + str(test.get("exit_code"))
                         + "`，计数 `" + str(test.get("counts", {})) + "`。")
    if not verifications:
        lines.append("- 未找到独立验收报告；不将 Agent finish 或单次测试视为独立成功。")
    for path in sorted(directory.glob("verify-*/manifest.json")):
        legacy = _read(path)
        lines.append("- 旧格式验收：`" + str(path) + "`，原报告状态 `" + str(legacy.get("status", "未知"))
                     + "`，副本 `" + location(legacy.get("workspace")) + "`；保留原结论，不冒充新协议或本次重跑。")
        if legacy.get("agent_patch_sha256"):
            lines.append("- 旧验收记录 P SHA256：`" + legacy["agent_patch_sha256"] + "`。")
    lines += ["", "## 未验证项与缺失材料", ""]
    for note in metadata.get("notes", []):
        lines.append("- " + note)
    if metadata.get("historical"):
        lines.append("- 本摘要仅归组既有证据；没有重跑模型或验收，也未改写原始材料。")
    if not receipt:
        lines.append("- 未找到标准 agent-run.json；其他旧材料不由本摘要推断为标准运行记录。")
    if not baseline:
        lines.append("- 缺少标准 G+R manifest，历史材料保留但不补造。")
    if baseline.get("error"):
        lines.append("- 准备失败原因：" + str(baseline["error"]).replace("\n", " "))
    for label, record in (("总流程", total), ("Agent", receipt), ("Agent 结果", receipt.get("result", {}))):
        if record.get("error"):
            lines.append("- " + label + "失败原因：" + str(record["error"]).replace("\n", " "))
    if total.get("verification_skip_reason"):
        lines.append("- 验收跳过原因：`" + str(total["verification_skip_reason"]) + "`。")
    lines += ["- 工作副本隔离文件，不代表执行沙箱；模型重复生成同一补丁未验证。",
              "- runs/ 与 experiments/ 为本机忽略目录，不代表已备份到远端。", ""]
    temporary = directory / "summary.md.tmp"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(directory / "summary.md")
    index = ["# 实验轮次索引", "", "按目录名显示北京时间；具体结论以各轮原始证据为准。", ""]
    for path in sorted(directory.parent.glob("*/round.json")):
        item = _read(path)
        index.append(f"- [{path.parent.name}]({path.parent.name}/summary.md) — 入口 `{item.get('entry', '未知')}`")
    index_path = directory.parent / "INDEX.md"
    # Unique temp name also allows independent rounds to finish concurrently.
    temporary = index_path.with_name("INDEX." + uuid.uuid4().hex + ".tmp")
    temporary.write_text("\n".join(index) + "\n", encoding="utf-8")
    temporary.replace(index_path)
