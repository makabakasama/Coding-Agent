"""Authorized historical regrouping. Original evidence bytes are never rewritten."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_agent.experiments import _files
from repo_agent.path_migrations import recorded_path, validate_mappings
from repo_agent.round_layout import BEIJING, initialize_round, new_round_id, workspace_name, write_summary

MAP = ROOT / "experiments/path-migrations.json"
REPORT = ROOT / "runs/migrations" / ("rounds_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(path, *args):
    proc = subprocess.run(["git", "-c", "safe.directory=" + path.as_posix(), "-c", "core.longpaths=true",
        "-c", "core.autocrlf=false", "-C", str(path), *args], capture_output=True, timeout=120)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors="replace"))
    return proc.stdout


def inventory(path):
    return {"head": git(path, "rev-parse", "HEAD").decode().strip(),
        "status": git(path, "status", "--porcelain=v1", "--untracked-files=all").decode(),
        "diff": sha(git(path, "diff", "--binary", "--no-ext-diff", "HEAD", "--")),
        "index_diff": sha(git(path, "diff", "--cached", "--binary", "--no-ext-diff", "--")), "files": _files(path)}


def bounded(path):
    path = path.resolve()
    if not any(path != ROOT / area and path.is_relative_to(ROOT / area) for area in ("experiments", "runs")):
        raise ValueError("Path outside authorized areas: " + str(path))
    return path


def move(old, new):
    old, new = bounded(old), bounded(new)
    if not old.is_dir() or new.exists():
        raise ValueError("Missing source or destination conflict: " + str(old) + " -> " + str(new))
    new.parent.mkdir(parents=True, exist_ok=True)
    quote = lambda path: "'" + str(path).replace("'", "''") + "'"
    subprocess.run(["powershell", "-NoProfile", "-Command", "$ErrorActionPreference='Stop'; Move-Item -LiteralPath "
                    + quote(old) + " -Destination " + quote(new)], check=True, capture_output=True, timeout=120)


def identifier(name):
    match = re.search(r"(\d{8}T\d{6}Z)_([a-f0-9]{8})", name)
    if match:
        stamp = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(BEIJING)
        return stamp.strftime("%Y%m%d-%H%M%S") + "_" + match[2]
    return new_round_id()


def plan():
    groups, moves, linked, owners = [], [], {}, {}
    for project, name in (("browser-use", "browser-use-5998"), ("pydantic-ai", "pydantic-ai-10525")):
        parent = ROOT / "runs" / name
        records = sorted(path for path in parent.iterdir() if path.is_dir())
        used = set()
        def group(entry, original, notes=None):
            item = {"entry": entry, "round_id": identifier(original.name), "experiment": name,
                    "historical": True, "notes": notes or [], "records": [], "workspace_moves": []}
            item["directory"] = str(parent / (entry + "_" + item["round_id"]))
            groups.append(item)
            return item
        def attach(item, old, subdir):
            if old in used:
                raise ValueError("Historical record assigned twice: " + str(old))
            used.add(old)
            destination = Path(item["directory"]) / subdir if subdir else Path(item["directory"])
            moves.append((old, destination))
            item["records"].append({"old": str(old), "new": str(destination)})
            owners[old] = item
        def workspace(item, value, role):
            if not value:
                return
            old = recorded_path(value, MAP)
            if not old.is_dir():
                item["notes"].append("记录中的副本不存在（未创建或原运行失败），未补造：" + value)
                return
            new = ROOT / "experiments" / project / "workspaces" / workspace_name(name, item["round_id"], role)
            if old in linked:
                raise ValueError("Workspace assigned twice: " + str(old))
            linked[old] = new
            item["workspace_moves"].append({"old": str(old), "new": str(new), "role": role})
        # A pipeline's explicit preparation link defines its repair group.
        for old in records:
            if (old / "experiment.json").is_file():
                report = read(old / "experiment.json")
                item = group("run", old)
                attach(item, old, "")
                repair = recorded_path(report["prepare_manifest"], MAP).parent
                attach(item, repair, "repair")
                workspace(item, read(repair / "manifest.json").get("workspace"), "agent")
        # Standalone preparation/claimed runs retain their initial material type.
        for old in records:
            if old not in used and (old / "manifest.json").is_file():
                report = read(old / "manifest.json")
                if report.get("status") == "independently_verified":
                    continue
                item = group("prepare", old, ["未找到关联 --run 总报告；按独立准备材料归组，不推断原始 CLI 参数或未留存运行。"])
                attach(item, old, "repair")
                workspace(item, report.get("workspace"), "agent")
        # Verification identity comes from its saved manifest link, never time proximity.
        for old in records:
            if old in used:
                continue
            report_path = old / "verification.json"
            legacy = False
            if not report_path.is_file() and (old / "manifest.json").is_file():
                report_path, legacy = old / "manifest.json", True
            if not report_path.is_file():
                continue
            report = read(report_path)
            original = report.get("original_manifest") or report.get("baseline_manifest")
            owner = owners.get(recorded_path(original, MAP).parent) if original else None
            if owner is None:
                continue
            number = 1 + sum(Path(item["new"]).name.startswith("verify-") for item in owner["records"])
            role = f"verify-{number:02d}"
            attach(owner, old, role)
            workspace(owner, report.get("workspace"), role)
            if legacy:
                owner["notes"].append("历史独立报告使用旧格式，原件位于 " + role + "/manifest.json；不将其伪装为新协议验收。")
        for old in records:
            if old not in used:
                item = group("legacy", old, ["缺少可关联的标准 manifest/receipt/report；保留原始材料，不推断修复或验收结果。"])
                attach(item, old, "repair")
        for old in sorted((ROOT / "experiments" / project / "workspaces").iterdir()):
            if old.is_dir() and old not in linked:
                item = group("legacy", old, ["此副本没有可追溯的关联记录，标记 legacy-unlinked，不补造 G/R/P 或成功结果。"])
                if not re.search(r"\d{8}T\d{6}Z_", old.name):
                    item["notes"].append("名称中的时间为整理时间；原执行时间未知。")
                workspace(item, str(old), "legacy-unlinked")
                item["workspace"] = str(old)
    return groups, moves, linked


def main():
    REPORT.mkdir(parents=True, exist_ok=False)
    state = {"status": "planning", "events": []}
    try:
        groups, moves, linked = plan()
        mapping = read(MAP)
        (REPORT / "path-migrations-before.json").write_bytes(MAP.read_bytes())
        mapping["mappings"] += [{"old": str(a), "new": str(b)} for a, b in [*moves, *linked.items()]]
        mapping["report"] = str(REPORT / "report.json")
        validate_mappings(mapping, ROOT)
        for old, new in [*moves, *linked.items()]:
            bounded(old); bounded(new)
            if not old.is_dir() or new.exists():
                raise ValueError("Preflight conflict: " + str(old) + " -> " + str(new))
        roots = [Path(item["directory"]) for item in groups]
        if len(set(roots)) != len(roots) or any(path.exists() for path in roots):
            raise ValueError("Round destination conflict")
        save(REPORT / "plan.json", {"groups": groups, "record_moves": [(str(a), str(b)) for a, b in moves],
                                    "workspace_moves": [(str(a), str(b)) for a, b in linked.items()]})
        history = {str(path): sha(path.read_bytes()) for name in ("browser-use-5998", "pydantic-ai-10525", "live-smoke")
                   for path in (ROOT / "runs" / name).rglob("*") if path.is_file()}
        save(REPORT / "history-before.json", history)
        sources = [ROOT / "experiments" / project / "inputs/source" for project in ("browser-use", "pydantic-ai")]
        snapshots = {str(path): inventory(path) for path in [*linked, *sources]}
        save(REPORT / "checkouts-before.json", snapshots)
        state.update(status="moving", rounds=len(groups), workspace_count=len(linked))
        save(REPORT / "report.json", state)
        for old, new in linked.items():
            source = ROOT / "experiments" / old.parent.parent.name / "inputs/source"
            git(source, "worktree", "move", str(old), str(new))
            state["events"].append({"workspace": str(old), "to": str(new)})
            save(REPORT / "report.json", state)
        # Pipeline roots are moved before children; standalone roots are reserved first.
        pipeline_roots = {new for _, new in moves if new in roots}
        for root in roots:
            if root not in pipeline_roots:
                root.mkdir()
        for old, new in moves:
            move(old, new)
            state["events"].append({"records": str(old), "to": str(new)})
            save(REPORT / "report.json", state)
        for item in groups:
            initialize_round(Path(item["directory"]), item["round_id"], item["entry"], item["experiment"],
                             historical=True, notes=item["notes"], workspace=item.get("workspace"))
        after = {str(linked.get(Path(old), Path(old))): inventory(linked.get(Path(old), Path(old))) for old in snapshots}
        save(REPORT / "checkouts-after.json", after)
        for old, before in snapshots.items():
            if before != after[str(linked.get(Path(old), Path(old)))]:
                raise ValueError("Checkout changed: " + old)
        # Browser editable metadata points at the seed worktree; rewrite paths only.
        backups = REPORT / "environment-originals"
        edits = []
        site = ROOT / "experiments/browser-use/inputs/env/Lib/site-packages"
        for path in [*site.glob("*.pth"), *site.glob("*.dist-info/direct_url.json")]:
            original = path.read_bytes()
            changed = original
            for old, new in linked.items():
                for before, replacement in ((str(old), str(new)), (old.as_posix(), new.as_posix()), (old.as_uri(), new.as_uri())):
                    changed = changed.replace(before.encode(), replacement.encode())
            if changed != original:
                target = backups / path.relative_to(site)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original)
                path.write_bytes(changed)
                edits.append({"path": str(path), "before_sha256": sha(original), "after_sha256": sha(changed)})
        temporary = MAP.with_suffix(".json.tmp")
        save(temporary, mapping)
        temporary.replace(MAP)
        checked = {}
        for old, digest in history.items():
            current = recorded_path(old, MAP)
            if sha(current.read_bytes()) != digest:
                raise ValueError("Historical bytes changed: " + old)
            checked[old] = {"current": str(current), "sha256": digest}
        save(REPORT / "history-checked.json", checked)
        for item in groups:
            write_summary(Path(item["directory"]), MAP)
        state.update(status="completed", historical_files=len(history), historical_bytes_unchanged=True,
                     checkouts=len(snapshots), environment_metadata_edits=edits,
                     old_paths_absent=all(not path.exists() for path in [*linked, *(old for old, _ in moves)]))
    except BaseException as exc:
        state.update(status="failed", error_type=type(exc).__name__, error=str(exc),
                     recovery="Inspect plan and events; do not rerun blindly, delete results or reset worktrees.")
        raise
    finally:
        save(REPORT / "report.json", state)
        print(str(REPORT / "report.json"))


if __name__ == "__main__":
    main()
