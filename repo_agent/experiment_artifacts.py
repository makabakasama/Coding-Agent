"""Export a candidate P and audit a run without deciding that the repair passed."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .experiments import _files, _git, _sha


def export_run_artifacts(baseline: dict, receipt: dict) -> dict:
    """Retain results on failure too; never reset, stage, commit, or run tests."""
    directory = Path(baseline["manifest"]).resolve().parent
    destination = directory / "artifacts"
    destination.mkdir(exist_ok=False)
    report = {
        "status": "exporting", "agent_status": receipt["status"],
        "report": str(destination / "report.json"), "eligible_for_verification": False,
        "independently_verified": False, "test_runs": [],
    }

    def save():
        temporary = destination / "report.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination / "report.json")

    save()
    try:
        report["stage"] = "baseline_evidence"
        manifest_path = Path(baseline["manifest"])
        if _sha(manifest_path.read_bytes()) != receipt["manifest_sha256"]:
            raise ValueError("Preparation manifest changed after the run was claimed")
        for filename, key in (("baseline-files.json", "baseline_files_sha256"),
                              ("g-plus-r.patch", "g_plus_r_patch_sha256"),
                              ("regression.patch", "regression_patch_sha256")):
            if baseline.get(key) is not None and _sha((directory / filename).read_bytes()) != baseline[key]:
                raise ValueError("Saved baseline evidence changed: " + filename)
        before = json.loads((directory / "baseline-files.json").read_text(encoding="utf-8"))
        workspace = Path(baseline["workspace"]).resolve(strict=True)
        allowed = receipt["editable_paths"]
        if not allowed:
            raise ValueError("Run has no recorded implementation edit policy")
        for name in allowed:
            if (Path(name).is_absolute() or ".." in Path(name).parts or Path(name).as_posix() != name
                    or not isinstance(before.get(name), dict) or "sha256" not in before[name]):
                raise ValueError("Invalid implementation edit policy: " + name)
            if _sha(_git(workspace, "show", baseline["base_commit"] + ":" + name)) != before[name]["sha256"]:
                raise ValueError("R overlaps an editable implementation file: " + name)
        report["stage"] = "capture_changes"
        current = _files(workspace)
        changed = sorted(name for name in before.keys() | current.keys()
                         if before.get(name) != current.get(name))
        head = _git(workspace, "rev-parse", "HEAD").decode().strip()
        staged = _git(workspace, "diff", "--cached", "--name-only").decode("utf-8", errors="replace").splitlines()
        (destination / "final-files.json").write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        # This full diff includes R and is evidence only, never the candidate P.
        workspace_diff = _git(workspace, "diff", "--binary", "--no-ext-diff", "--no-renames",
                              baseline["base_commit"], "--")
        (destination / "workspace-vs-g.patch").write_bytes(workspace_diff)
        patch = _git(workspace, "diff", "--binary", "--no-ext-diff", "--no-renames",
                     baseline["base_commit"], "--", *allowed)
        (destination / "agent.patch").write_bytes(patch)
        report["patch"] = {"path": str(destination / "agent.patch"), "sha256": _sha(patch),
                           "bytes": len(patch), "changed_paths": [n for n in changed if n in allowed]}
        report["final_files_sha256"] = _sha((destination / "final-files.json").read_bytes())
        report["stage"] = "trace_audit"
        protected_edits, inconsistent_edits = [], []
        edit_hashes = {name: before[name]["sha256"] for name in allowed}
        trace_valid = False
        if receipt.get("trace"):
            trace = Path(receipt["trace"]).resolve(strict=True)
            if not trace.is_relative_to(directory / "agent"):
                raise ValueError("Trace is outside this run's agent directory")
            trace_bytes = trace.read_bytes()
            report["trace_sha256"] = _sha(trace_bytes)
            for line in trace_bytes.decode("utf-8").splitlines():
                event = json.loads(line)
                if event.get("type") != "observation":
                    continue
                result = event.get("result", {})
                data = result.get("data", {})
                if event.get("action") == "run_tests":
                    report["test_runs"].append({"step": event.get("step"), "result": result})
                if result.get("ok") and data.get("applied"):
                    paths = [data["path"]] if data.get("path") else re.findall(
                        r"^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\r\n ]+)", data.get("patch", ""), re.M)
                    if not paths or any(name != "/dev/null" and name not in allowed for name in paths):
                        protected_edits.append({"step": event.get("step"), "paths": paths})
                    name = data.get("path")
                    if (name in edit_hashes and data.get("consistent") is True
                            and data.get("before_sha256") == edit_hashes[name]
                            and isinstance(data.get("after_sha256"), str)
                            and re.fullmatch(r"[0-9a-f]{64}", data["after_sha256"])):
                        edit_hashes[name] = data["after_sha256"]
                    else:
                        inconsistent_edits.append(event.get("step"))
            trace_valid = True
        report["stage"] = "final_checks"
        trace_matches_final = all(current.get(name, {}).get("sha256") == digest
                                  for name, digest in edit_hashes.items())
        consistent = (_files(workspace) == current
                      and _git(workspace, "rev-parse", "HEAD").decode().strip() == head
                      and _git(workspace, "diff", "--cached", "--name-only").decode("utf-8", errors="replace").splitlines() == staged)
        report["integrity"] = {
            "head_unchanged": head == baseline["base_commit"], "index_empty": not staged,
            "protected_paths_changed": [n for n in changed if n not in allowed],
            "protected_edits_in_trace": protected_edits, "inconsistent_tool_edits": inconsistent_edits,
            "trace_available_and_valid": trace_valid, "snapshot_consistent": consistent,
            "trace_hashes_match_final_files": trace_matches_final,
        }
        valid = (head == baseline["base_commit"] and not staged and consistent
                 and not report["integrity"]["protected_paths_changed"]
                 and not protected_edits and not inconsistent_edits and trace_matches_final)
        if not valid:
            report["status"] = "integrity_violation"
        elif not patch:
            report["status"] = "no_code_change"
        elif not trace_valid:
            report["status"] = "incomplete_trace"
        else:
            report.update(status="candidate_exported", eligible_for_verification=True)
        report["stage"] = "complete"
    except Exception as exc:
        report.update(status="export_failed", error_type=type(exc).__name__, error=str(exc))
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    save()
    return report
