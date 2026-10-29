"""Disk-source evidence only; no credentials, model objects or environment dump."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import ssl
import subprocess
import sys
import uuid


SOURCE_ROOT = Path(__file__).resolve().parent.parent
ENTRY_SOURCES = ("examples/browser_use_5998.py", "examples/pydantic_ai_10525.py")
SOURCE_SCOPE = ["repo_agent/*.py (direct children only)", *ENTRY_SOURCES]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sources(root: Path) -> dict[str, bytes]:
    paths = []
    for directory in (root / "repo_agent", root / "examples"):
        if directory.is_symlink() or directory.resolve() != directory or not directory.is_dir():
            raise ValueError("Agent source directory is missing or symlinked")
    paths.extend(path for path in (root / "repo_agent").glob("*.py") if not path.name.startswith("."))
    paths.extend(root / name for name in ENTRY_SOURCES if (root / name).exists() or (root / name).is_symlink())
    if not any(path.relative_to(root).as_posix() in ENTRY_SOURCES for path in paths):
        raise ValueError("Agent experiment entry is missing")
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError("Agent source is missing or symlinked: " + path.name)
        result[path.relative_to(root).as_posix()] = path.read_bytes()
    if "repo_agent/agent.py" not in result:
        raise ValueError("Agent implementation is missing")
    return result


def _git_metadata(root: Path, paths: list[str]) -> dict:
    def git(*args):
        return subprocess.run(["git", "--no-optional-locks", "-c", "safe.directory=" + root.as_posix(),
                               "-C", str(root), *args], capture_output=True, timeout=20)
    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode:
            return {"available": False, "reason": "not_a_git_repository"}
        head = git("rev-parse", "--verify", "HEAD")
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths)
        if status.returncode:
            return {"available": False, "reason": "source_status_unavailable"}
        return {"available": True, "root": top.stdout.decode("utf-8", errors="replace").strip(),
                "head": head.stdout.decode().strip() if head.returncode == 0 else None,
                "source_status_records": status.stdout.decode("utf-8", errors="replace").split("\0")[:-1]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": type(exc).__name__}


def archive_agent_source(destination: Path, *, source_root: Path | None = None) -> dict:
    """Exclusive new archive. source_root is injectable for synthetic fixtures only."""
    root = (source_root or SOURCE_ROOT).resolve(strict=True)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "capturing", "manifest": str(destination / "manifest.json"),
                "source_root": str(root), "scope": SOURCE_SCOPE, "files": {},
                "snapshot_kind": "allowlisted_disk_source", "loaded_code_equivalence": False,
                "runtime": {"python": sys.executable, "python_version": sys.version,
                            "platform": platform.platform(), "openssl": ssl.OPENSSL_VERSION},
                "started_at_utc": datetime.now(timezone.utc).isoformat()}
    interrupted = False
    try:
        sources = _sources(root)
        for name, data in sources.items():
            target = destination / "source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
            manifest["files"][name] = {"sha256": sha256(data), "bytes": len(data)}
        manifest["git"] = _git_metadata(root, list(sources))
        after = _sources(root)
        changed = sorted(name for name in sources.keys() | after.keys() if sources.get(name) != after.get(name))
        manifest.update(status="source_drift" if changed else "captured", changed_during_capture=changed)
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        manifest.update(status="capture_interrupted" if interrupted else "capture_failed", error_type=type(exc).__name__)
    manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    with Path(manifest["manifest"]).open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    if interrupted:
        raise KeyboardInterrupt
    return manifest


def source_archive_summary(path: Path) -> dict:
    data = path.read_bytes()
    return {"status": json.loads(data)["status"], "manifest": str(path), "manifest_sha256": sha256(data)}


def check_agent_source(summary: dict) -> dict:
    """Write a separate check; never overwrite the initial source snapshot."""
    path = Path(summary["manifest"])
    target = path.parent / ("source-check_" + uuid.uuid4().hex + ".json")
    result = {"report": str(target), "source_manifest": str(path),
              "checked_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        data = path.read_bytes()
        if sha256(data) != summary["manifest_sha256"]:
            raise ValueError("Initial source manifest hash changed")
        manifest = json.loads(data)
        for name, info in manifest["files"].items():
            if (name not in ENTRY_SOURCES
                    and not (name.startswith("repo_agent/") and len(Path(name).parts) == 2
                             and Path(name).suffix == ".py")):
                raise ValueError("Source inventory is outside the allowed scope")
            if sha256((path.parent / "source" / name).read_bytes()) != info["sha256"]:
                raise ValueError("Archived source bytes changed")
        current = {name: sha256(value) for name, value in _sources(Path(manifest["source_root"])).items()}
        before = {name: info["sha256"] for name, info in manifest["files"].items()}
        changed = sorted(name for name in before.keys() | current.keys() if before.get(name) != current.get(name))
        result.update(status="source_drift" if changed else "unchanged", changed_paths=changed)
    except Exception as exc:
        result.update(status="check_failed", error_type=type(exc).__name__)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result
