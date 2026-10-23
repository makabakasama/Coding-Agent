"""Create a disposable detached checkout at a recorded commit (not a sandbox)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


def create_worktree(source: Path, destination: Path, revision: str) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Destination already exists; choose a new directory")
    if not revision or revision.startswith("-"):
        raise ValueError("A commit or revision is required")

    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=120).stdout.strip()

    commit = git("rev-parse", "--verify", revision + "^{commit}")
    git("worktree", "add", "--detach", str(destination), commit)
    return {"source": str(source), "workspace": str(destination),
            "base_commit": commit, "is_execution_sandbox": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        result = create_worktree(args.source, args.destination, args.revision)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
