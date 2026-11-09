"""Explicit, local relocation records; never rewrite historical evidence bytes."""
from __future__ import annotations

import json
import os
from pathlib import Path


def mappings(manifest: Path | None) -> list[tuple[Path, Path]]:
    if manifest is None or not manifest.exists():
        return []
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return validate_mappings(data, manifest.resolve().parent.parent)


def validate_mappings(data: dict, root: Path) -> list[tuple[Path, Path]]:
    if data.get("schema") != 1 or data.get("status") != "completed":
        raise ValueError("Path migration is not completed")
    areas = (root / "experiments", root / "runs")
    result = []
    for item in data["mappings"]:
        old, new = Path(item["old"]).resolve(), Path(item["new"]).resolve()
        if (old == new or not any(old != area and new != area and old.is_relative_to(area)
                                 and new.is_relative_to(area) for area in areas)):
            raise ValueError("Migration paths must remain inside experiments or runs, below their roots")
        if any(old == prior or old.is_relative_to(prior) or prior.is_relative_to(old)
               for prior, _ in result):
            raise ValueError("Ambiguous migration source")
        if any(new == prior for _, prior in result):
            raise ValueError("Duplicate migration destination")
        result.append((old, new))
    for old, _ in result:
        _follow(old, result)
    return result


def _follow(path: Path, pairs: list[tuple[Path, Path]]) -> Path:
    seen = set()
    for _ in range(len(pairs) + 1):
        match = next(((old, new) for old, new in pairs if path.is_relative_to(old)), None)
        if match is None:
            return path.resolve()
        old, new = match
        if old in seen:
            raise ValueError("Cyclic migration mapping")
        seen.add(old)
        path = (new / path.relative_to(old)).resolve()
    raise ValueError("Cyclic migration mapping")


def recorded_path(value: str | Path, manifest: Path | None = None) -> Path:
    return _follow(Path(value).resolve(), mappings(manifest))


def recorded_environment(value: dict[str, str], manifest: Path | None = None) -> dict[str, str]:
    # Environment values used by the experiment are paths or platform path lists.
    # Do not perform arbitrary substring replacement (foo must not match foo-old).
    if manifest is None or not manifest.exists():
        return dict(value)
    return {key: os.pathsep.join(str(recorded_path(part, manifest)) if Path(part).is_absolute() else part
                                for part in text.split(os.pathsep))
            for key, text in value.items()}
