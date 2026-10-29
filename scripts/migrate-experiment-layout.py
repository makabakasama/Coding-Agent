"""One-shot authorized local migration. No network, installs, models or target tests.

Run with the project's tooling Python. Stops on conflicts; never overwrites a
destination or deletes a source. A failed report records the last completed step.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_agent.experiments import _files

AREA = ROOT / "experiments"
REPORT = ROOT / "runs/migrations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def run(args, **kwargs):
    result = subprocess.run([str(x) for x in args], capture_output=True, timeout=180, **kwargs)
    if result.returncode:
        raise RuntimeError(str(args[:4]) + ": " + result.stderr.decode(errors="replace"))
    return result.stdout


def git(path, *args):
    return run(["git", "-c", "safe.directory=" + path.as_posix(), "-c", "core.longpaths=true",
                "-c", "core.autocrlf=false", "-C", path, *args])


def bounded(path):
    path = path.resolve()
    if path == AREA.resolve() or not path.is_relative_to(AREA.resolve()) or path.is_symlink():
        raise ValueError("Unsafe migration path: " + str(path))
    return path


def move(old, new):
    old, new = bounded(old), bounded(new)
    if not old.is_dir() or new.exists():
        raise ValueError("Missing source or conflicting destination: " + str(old) + " -> " + str(new))
    new.parent.mkdir(parents=True, exist_ok=True)
    # One native PowerShell filesystem operation end-to-end, with literal paths.
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    run(["powershell", "-NoProfile", "-Command", "$ErrorActionPreference='Stop'; Move-Item -LiteralPath "
         + quote(old) + " -Destination " + quote(new)])


def checkout(path):
    return {"head": git(path, "rev-parse", "HEAD").decode().strip(),
            "status": git(path, "status", "--porcelain=v1", "--untracked-files=all").decode(),
            "diff": digest(git(path, "diff", "--binary", "--no-ext-diff", "HEAD", "--")),
            "index_diff": digest(git(path, "diff", "--cached", "--binary", "--no-ext-diff", "--")),
            "files": _files(path)}


PROBE = """import sys,json,importlib.metadata as m
print(json.dumps({'version':sys.version,'base':sys._base_executable,'executable':sys.executable,'prefix':sys.prefix,
'packages':sorted((d.metadata['Name'],d.version) for d in m.distributions()),
'entrypoints':sorted((ep.name,ep.value) for d in m.distributions() for ep in d.entry_points if ep.group=='console_scripts')}))
"""


def environment(path):
    return json.loads(run([path / "Scripts/python.exe", "-c", PROBE]))


def rewritten(data, mapping):
    for old, new in mapping:
        variants = [(str(old), str(new)), (old.as_posix(), new.as_posix()),
                    (str(old).replace("\\", "\\\\"), str(new).replace("\\", "\\\\")),
                    (old.as_uri(), new.as_uri())]
        for before, after in variants:
            data = data.replace(before.encode(), after.encode())
    return data


def repair_environment(old, new, before, mapping, label):
    backup = REPORT / "environment-originals" / label
    backup.mkdir(parents=True)
    shutil.copytree(new / "Scripts", backup / "Scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(new / "pyvenv.cfg", backup / "pyvenv.cfg")
    cfg = (new / "pyvenv.cfg").read_bytes()
    # Rebuild only the interpreter launchers and standard activation skeleton.
    # --without-pip leaves every installed distribution untouched.
    run([before["base"], "-m", "venv", "--without-pip", new])
    (new / "pyvenv.cfg").write_bytes(rewritten(cfg, mapping))
    edits = []
    candidates = list((new / "Scripts").glob("*"))
    site = new / "Lib/site-packages"
    candidates += list(site.glob("*.pth")) + list(site.glob("*.egg-link"))
    candidates += list(site.glob("_editable*.py")) + list(site.glob("*.dist-info/direct_url.json"))
    for path in candidates:
        if not path.is_file() or path.suffix.lower() in (".exe", ".dll", ".pyc"):
            continue
        data = path.read_bytes()
        updated = rewritten(data, mapping)
        if data != updated:
            target = backup / "metadata" / path.relative_to(new)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            path.write_bytes(updated)
            edits.append({"path": str(path), "before": digest(data), "after": digest(updated)})
    # Generate distlib launchers from distribution metadata, never patch binaries.
    regenerate = """import json,sys
from pip._vendor.distlib.scripts import ScriptMaker
data=json.loads(sys.stdin.read()); maker=ScriptMaker(None,data['scripts']); maker.executable=data['python']; maker.clobber=True; maker.variants={''}
for name,value in data['entrypoints']:
    maker.make(name+' = '+value)
"""
    entries = dict(before["entrypoints"])
    if "pip" in entries:
        for name in ("pip3", "pip" + ".".join(before["version"].split()[0].split(".")[:2])):
            entries[name] = entries["pip"]
    # Existing package entry points only; no new optional applications.
    selected = [(name, value) for name, value in entries.items() if (new / "Scripts" / (name + ".exe")).exists()]
    run([sys.executable, "-c", regenerate], input=json.dumps({"scripts": str(new / "Scripts"),
        "python": str(new / "Scripts/python.exe"), "entrypoints": selected}).encode())
    stale = []
    for path in (new / "Scripts").glob("*"):
        if path.is_file() and any(str(src).encode() in path.read_bytes() for src, _ in mapping):
            stale.append(str(path))
    if stale:
        raise ValueError("Unrepaired Scripts references: " + repr(stale))
    after = environment(new)
    if before["packages"] != after["packages"] or before["version"] != after["version"]:
        raise ValueError("Environment versions changed: " + label)
    if Path(after["prefix"]).resolve() != new.resolve() or Path(after["executable"]).resolve() != new / "Scripts/python.exe":
        raise ValueError("Environment prefix/executable mismatch")
    checks = {}
    for module, args in (("pytest", ["--version"]), ("pip", ["--version"]), ("uv", ["--version"])):
        if any(name.lower().replace("_", "-") == module for name, _ in before["packages"]):
            checks[module] = run([new / "Scripts/python.exe", "-m", module, *args]).decode().strip()
    if (new / "Scripts/pytest.exe").exists():
        checks["pytest_launcher"] = run([new / "Scripts/pytest.exe", "--version"]).decode().strip()
    save(REPORT / (label + "-after.json"), after)
    return {"versions_preserved": True, "metadata_edits": edits, "launchers_rebuilt": len(selected), "checks": checks}


def main():
    REPORT.mkdir(parents=True, exist_ok=False)
    state = {"status": "planning", "report": str(REPORT), "events": []}
    save(REPORT / "report.json", state)
    try:
        linked = sorted(p for p in AREA.glob("browser-use-5998*") if p.is_dir())
        mapping = [(p, AREA / "browser-use/workspaces" / p.name) for p in linked]
        mapping += [(AREA / "browser-use-source", AREA / "browser-use/inputs/source"),
                    (AREA / "pydantic-ai-10525-source", AREA / "pydantic-ai/inputs/source"),
                    (AREA / "browser-use-env", AREA / "browser-use/inputs/env"),
                    (AREA / "pydantic-ai-10525-env", AREA / "pydantic-ai/inputs/env"),
                    (AREA / "pydantic-ai-10525-tools", AREA / "pydantic-ai/inputs/tools-env")]
        for old, new in mapping:
            bounded(old); bounded(new)
            if not old.is_dir() or new.exists():
                raise ValueError("Preflight conflict: " + str(old) + " -> " + str(new))
        if (AREA / "path-migrations.json").exists():
            raise ValueError("Migration map already exists")
        mapdata = {"schema": 1, "status": "planned", "report": str(REPORT / "report.json"),
                   "mappings": [{"old": str(a), "new": str(b)} for a, b in mapping]}
        save(REPORT / "plan.json", mapdata)
        history = {str(p.relative_to(ROOT)): digest(p.read_bytes()) for folder in
                   ("browser-use-5998", "pydantic-ai-10525", "live-smoke")
                   for p in (ROOT / "runs" / folder).rglob("*") if p.is_file()}
        save(REPORT / "history-before.json", history)
        repos = [*linked, AREA / "browser-use-source", AREA / "pydantic-ai-10525-source"]
        snapshots = {str(p): checkout(p) for p in repos}
        save(REPORT / "checkouts-before.json", snapshots)
        envs = {str(old): environment(old) for old, _ in mapping[-3:]}
        save(REPORT / "environments-before.json", envs)
        state["status"] = "moving"
        save(REPORT / "report.json", state)
        source = AREA / "browser-use-source"
        for old, new in mapping[:len(linked)]:
            new.parent.mkdir(parents=True, exist_ok=True)
            git(source, "worktree", "move", str(old), str(new))
            state["events"].append({"moved": str(old), "to": str(new)})
            save(REPORT / "report.json", state)
        for old, new in mapping[len(linked):]:
            move(old, new)
            state["events"].append({"moved": str(old), "to": str(new)})
            save(REPORT / "report.json", state)
        newsource = AREA / "browser-use/inputs/source"
        git(newsource, "worktree", "repair", *[str(new) for _, new in mapping[:len(linked)]])
        after = {str(new): checkout(new) for old, new in mapping if str(old) in snapshots}
        save(REPORT / "checkouts-after.json", after)
        for old, new in mapping:
            if str(old) in snapshots and snapshots[str(old)] != after[str(new)]:
                raise ValueError("Checkout contents/state changed: " + str(new))
        state["checkout_count"] = len(snapshots)
        state["environment_checks"] = {}
        for old, new in mapping[-3:]:
            state["environment_checks"][new.parent.parent.name + "-" + new.name] = repair_environment(
                old, new, envs[str(old)], mapping, old.name)
            save(REPORT / "report.json", state)
        history_after = {str(p.relative_to(ROOT)): digest(p.read_bytes()) for folder in
                         ("browser-use-5998", "pydantic-ai-10525", "live-smoke")
                         for p in (ROOT / "runs" / folder).rglob("*") if p.is_file()}
        save(REPORT / "history-after.json", history_after)
        if history != history_after:
            raise ValueError("Historical evidence changed")
        state.update(status="completed", historical_files=len(history), history_unchanged=True,
                     old_paths_absent=all(not old.exists() for old, _ in mapping))
        mapdata["status"] = "completed"
        save(AREA / "path-migrations.json", mapdata)
    except BaseException as exc:
        state.update(status="failed", error_type=type(exc).__name__, error=str(exc),
                     recovery="Do not rerun blindly or delete partial results. Inspect plan/events and repair remaining steps.")
        raise
    finally:
        save(REPORT / "report.json", state)
        print(str(REPORT / "report.json"), flush=True)


if __name__ == "__main__":
    main()
