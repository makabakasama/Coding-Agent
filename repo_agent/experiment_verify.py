"""Independently replay a saved candidate on a fresh G+R; never call a model."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import uuid
from .path_migrations import recorded_path, recorded_environment
from .round_layout import ID_PATTERN, allocate_round, allocate_verification, find_round, write_summary

from .experiments import (ExperimentSpec, _files, _git, _read_provenance,
                          _run_import_probe, _run_test, _sha, prepare_experiment, experiment_environment)


def _object(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Experiment evidence must be a JSON object")
    return value


def _read_candidate(path: Path, spec: ExperimentSpec, destination: Path, evidence: dict) -> dict:
    """Read frozen materials, not the former Agent workspace's current files."""
    path = recorded_path(path, spec.path_migrations).resolve(strict=True)
    directory = path.parent
    migration_bytes = None
    if spec.path_migrations is not None and spec.path_migrations.exists():
        migration_bytes = spec.path_migrations.read_bytes()
        (destination / "path-migrations.json").write_bytes(migration_bytes)
        evidence["path_migrations"] = {"path": str(spec.path_migrations.resolve()),
                                       "sha256": _sha(migration_bytes)}
    def located(value):
        return recorded_path(value, spec.path_migrations)
    inputs = destination / "inputs"
    inputs.mkdir()

    def read(name: str, digest: str | None = None) -> bytes:
        source = directory / name
        data = source.read_bytes()
        target = inputs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        evidence[name] = {"path": str(source), "sha256": _sha(data)}
        if digest is not None and _sha(data) != digest:
            raise ValueError("Saved material hash mismatch: " + name)
        return data

    baseline_bytes = read("manifest.json")
    baseline = _object(baseline_bytes)
    receipt = _object(read("agent-run.json"))
    if receipt.get("manifest_sha256") != _sha(baseline_bytes):
        raise ValueError("Preparation manifest changed after claim")
    if (located(baseline["manifest"]) != path
            or located(receipt["manifest"]) != path
            or receipt.get("workspace") != baseline["workspace"]):
        raise ValueError("Manifest/run identity mismatch")
    if (baseline.get("status") != "baseline_verified" or baseline.get("stage") != "complete"
            or baseline.get("baseline_files_unchanged") is not True):
        raise ValueError("Original baseline was not verified")
    terminal = {"agent_reported_finished", "step_limit", "error", "startup_or_run_error", "interrupted"}
    status = receipt.get("status")
    if status not in terminal or receipt.get("result", {}).get("status") != status:
        raise ValueError("Agent run has no consistent terminal status")
    base = _git(spec.source.resolve(strict=True), "rev-parse", "--verify",
                spec.revision + "^{commit}").decode().strip()
    fields = {
        "name": spec.name, "source": str(spec.source.resolve()), "base_commit": base,
        "test_python": str(spec.test_python.resolve(strict=True)),
        "regression_target": spec.regression_target, "control_targets": list(spec.control_targets),
        "failure_contains": spec.failure_contains, "import_module": spec.import_module,
        "test_timeout": spec.test_timeout,
        "test_environment": experiment_environment(spec, directory, located(baseline["workspace"])),
    }
    for key, expected in fields.items():
        actual = baseline.get(key)
        if key in ("source", "test_python"):
            actual = str(located(actual))
        elif key == "test_environment":
            actual = recorded_environment(actual, spec.path_migrations)
            expected = recorded_environment(expected, spec.path_migrations)
        if actual != expected:
            raise ValueError("Recorded experiment configuration differs: " + key)
    provenance = baseline["provenance"]
    if (located(provenance["python"]) != spec.test_python.resolve()
            or not located(provenance["module_file"]).is_relative_to(located(baseline["workspace"]))
            or not isinstance(provenance["packages"], dict) or not provenance.get("version")):
        raise ValueError("Original import provenance is inconsistent")
    allowed = list(spec.editable_paths)
    if not allowed or len(set(allowed)) != len(allowed) or receipt.get("editable_paths") != allowed:
        raise ValueError("Recorded implementation edit policy differs")
    before = _object(read("baseline-files.json", baseline["baseline_files_sha256"]))
    if not before:
        raise ValueError("Empty baseline inventory")
    read("g-plus-r.patch", baseline["g_plus_r_patch_sha256"])
    regression = None
    if spec.regression_patch is not None:
        expected_r = spec.regression_sha256 or _sha(spec.regression_patch.read_bytes())
        if baseline.get("regression_patch_sha256") != expected_r:
            raise ValueError("R differs from the frozen experiment")
        read("regression.patch", expected_r)
        regression = inputs / "regression.patch"
    elif baseline.get("regression_patch_sha256") is not None:
        raise ValueError("Unexpected R in original run")
    for name in allowed:
        if (Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name
                or Path(name).as_posix() != name
                or before.get(name) != {"sha256": _sha(_git(spec.source, "show", base + ":" + name))}):
            raise ValueError("Invalid editable file or R overlaps implementation: " + name)
    report = _object(read("artifacts/report.json"))
    if located(report["report"]) != directory / "artifacts/report.json":
        raise ValueError("Export report identity mismatch")
    summary = receipt.get("artifacts", {})
    for key in ("status", "report", "eligible_for_verification", "independently_verified"):
        if summary.get(key) != report.get(key):
            raise ValueError("Run/export summary mismatch: " + key)
    if (report.get("status") != "candidate_exported" or report.get("stage") != "complete"
            or report.get("eligible_for_verification") is not True
            or report.get("independently_verified") is not False
            or report.get("agent_status") != status):
        raise ValueError("Export is not eligible for independent verification")
    integrity = report["integrity"]
    for key in ("head_unchanged", "index_empty", "trace_available_and_valid", "snapshot_consistent",
                "trace_hashes_match_final_files"):
        if integrity.get(key) is not True:
            raise ValueError("Export integrity check failed: " + key)
    for key in ("protected_paths_changed", "protected_edits_in_trace", "inconsistent_tool_edits"):
        if integrity.get(key) != []:
            raise ValueError("Export integrity check failed: " + key)
    trace = located(receipt["trace"]).resolve(strict=True)
    if not trace.is_relative_to(directory / "agent"):
        raise ValueError("Trace is outside original Agent directory")
    read(trace.relative_to(directory).as_posix(), report["trace_sha256"])
    final = _object(read("artifacts/final-files.json", report["final_files_sha256"]))
    changed = sorted(name for name in before.keys() | final.keys() if before.get(name) != final.get(name))
    if not changed or any(name not in allowed for name in changed):
        raise ValueError("Final inventory changes protected files or has no code change")
    if any(set(final.get(name, {})) != {"sha256"} for name in allowed):
        raise ValueError("Implementation is missing or is not a regular file")
    candidate = report["patch"]
    if (located(candidate["path"]) != directory / "artifacts/agent.patch"
            or candidate.get("changed_paths") != changed):
        raise ValueError("Candidate identity or changed paths mismatch")
    patch = read("artifacts/agent.patch", candidate["sha256"])
    if not patch.strip() or candidate.get("bytes") != len(patch):
        raise ValueError("Empty candidate or patch size mismatch")
    if migration_bytes is not None and spec.path_migrations.read_bytes() != migration_bytes:
        raise ValueError("Path migration map changed while reading candidate")
    return {"baseline": baseline, "agent_status": status, "evidence": evidence,
            "before": before, "final": final, "patch": patch, "patch_sha256": _sha(patch),
            "regression": regression, "allowed": allowed, "changed": changed}


def _capture_git(root: Path, args: list[str], data: bytes, directory: Path, label: str) -> dict:
    command = ["git", "-c", "safe.directory=" + root.as_posix(), "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
               "-C", str(root), *args]
    try:
        proc = subprocess.run(command, input=data, capture_output=True, timeout=120)
        code, stdout, stderr, timed_out = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        code, stdout, stderr, timed_out = None, exc.stdout or b"", exc.stderr or b"", True
    (directory / (label + ".stdout.txt")).write_bytes(stdout)
    (directory / (label + ".stderr.txt")).write_bytes(stderr)
    return {"command": command, "exit_code": code, "timed_out": timed_out,
            "stdout": label + ".stdout.txt", "stderr": label + ".stderr.txt",
            "stdin_sha256": _sha(data)}


def verify_experiment(manifest_path: Path, *, expected_spec: ExperimentSpec,
                      workspace_parent: Path, trace_parent: Path,
                      round_directory: Path | None = None) -> dict:
    """Retain every attempt; original run records/workspaces are never changed."""
    spec = expected_spec
    manifest_path = recorded_path(manifest_path, spec.path_migrations)
    round_directory = round_directory or find_round(manifest_path)
    if round_directory is None:
        round_directory, _ = allocate_round(trace_parent / spec.name, "legacy", spec.name)
    round_directory = round_directory.resolve(strict=True)
    expected_parent = (trace_parent / spec.name).resolve()
    if round_directory == expected_parent or not round_directory.is_relative_to(expected_parent):
        raise ValueError("Verification round is outside the experiment records area")
    metadata = _object((round_directory / "round.json").read_bytes())
    if (metadata.get("experiment") != spec.name or not ID_PATTERN.fullmatch(str(metadata.get("round_id", "")))
            or metadata.get("entry") not in ("run", "prepare", "legacy")
            or round_directory.name != metadata["entry"] + "_" + metadata["round_id"]):
        raise ValueError("Verification round metadata does not match this experiment")
    run_id = metadata["round_id"]
    directory, role = allocate_verification(round_directory)
    report = {"status": "verifying", "stage": "validate_materials", "run_id": run_id,
              "report": str(directory / "verification.json"), "original_manifest": str(manifest_path.resolve()),
              "independently_verified": False, "model_started": False, "is_execution_sandbox": False,
              "tests": [], "patch_commands": [], "source_materials": {},
              "started_at_utc": datetime.now(timezone.utc).isoformat()}

    def save():
        target = directory / "verification.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    save()
    try:
        candidate = _read_candidate(manifest_path, spec, directory, report["source_materials"])
        report.update(agent_status=candidate["agent_status"], source_materials=candidate["evidence"],
                      patch_sha256=candidate["patch_sha256"], base_commit=candidate["baseline"]["base_commit"],
                      mandatory_targets=[spec.regression_target, *spec.control_targets])
        report["stage"] = "reproduce_g_plus_r"
        save()
        # Reuse the complete preparation chain, using only the saved R bytes.
        replay_spec = replace(spec, revision=report["base_commit"], regression_patch=candidate["regression"],
                              regression_sha256=candidate["baseline"].get("regression_patch_sha256"))
        baseline = prepare_experiment(replay_spec, workspace_parent=workspace_parent,
                                      trace_parent=directory / "baseline", round_directory=round_directory,
                                      round_id=run_id, record_subdir=role + "/baseline", workspace_role=role)
        report.update(baseline_manifest=baseline["manifest"], workspace=baseline["workspace"],
                      baseline_status=baseline["status"], baseline_tests=baseline["tests"],
                      test_environment=baseline["test_environment"], test_python=baseline["test_python"])
        save()
        if baseline["status"] != "baseline_verified":
            report["baseline_failure_stage"] = baseline["stage"]
            raise ValueError("Fresh G+R failed baseline verification: " + baseline.get("error", "unknown error"))
        workspace = Path(baseline["workspace"])
        if _files(workspace) != candidate["before"]:
            raise ValueError("Fresh G+R inventory differs from frozen original baseline")
        report["stage"] = "compare_environment"
        report["environment_comparison"] = {
            "python_version_matches": baseline["provenance"]["version"] == candidate["baseline"]["provenance"]["version"],
            "package_versions_match": baseline["provenance"]["packages"] == candidate["baseline"]["provenance"]["packages"],
        }
        save()
        if not all(report["environment_comparison"].values()):
            raise ValueError("Interpreter/package version metadata differs from the original run")
        report["stage"] = "check_patch_paths"
        save()
        stat = _git(workspace, "apply", "--numstat", "-z", "-", data=candidate["patch"])
        paths = [os.fsdecode(item.split(b"\t", 2)[2]) for item in stat.split(b"\0") if item]
        if (not paths or len(paths) != len(set(paths)) or sorted(paths) != candidate["changed"]
                or any(name not in candidate["allowed"] for name in paths)):
            raise ValueError("P touches an unexpected path")
        for label, arguments in (("patch-check", ["apply", "--check", "--whitespace=nowarn", "-"]),
                                 ("patch-apply", ["apply", "--whitespace=nowarn", "-"])):
            report["stage"] = label
            save()
            command = _capture_git(workspace, arguments, candidate["patch"], directory, label)
            report["patch_commands"].append(command)
            save()
            if command["exit_code"] != 0 or command["timed_out"]:
                raise ValueError("Saved P could not be checked/applied; see " + label + " logs")
        expected_final = candidate["final"]

        def check_files():
            current = _files(workspace)
            head = _git(workspace, "rev-parse", "HEAD").decode().strip()
            staged = _git(workspace, "diff", "--cached", "--name-only").decode().splitlines()
            changed = sorted(name for name in current.keys() | expected_final.keys()
                             if current.get(name) != expected_final.get(name))
            inventory = json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8")
            (directory / "observed-files.json").write_bytes(inventory)
            report["observed_files_sha256"] = _sha(inventory)
            report["integrity"] = {"head": head, "staged_paths": staged,
                                   "paths_differing_from_candidate": changed,
                                   "protected_paths_changed": [name for name in changed if name not in candidate["allowed"]]}
            if changed or head != report["base_commit"] or staged:
                raise ValueError("Final implementation/protected files, HEAD or index differ from saved candidate")
            return current

        report["stage"] = "check_applied_fingerprints"
        save()
        check_files()
        environment = {**os.environ, **baseline["test_environment"]}
        python = Path(baseline["test_python"])
        report["stage"] = "patched_import_provenance"
        save()
        probe = _run_import_probe(python, spec.import_module, workspace, environment,
                                  directory, "patched-import-probe", spec.test_timeout)
        report["import_probe"] = probe
        save()
        report["provenance"] = _read_provenance(probe, directory, workspace, python)
        check_files()
        for index, target in enumerate(report["mandatory_targets"]):
            report["stage"] = "patched_tests"
            save()
            result = _run_test(python, target, workspace, environment, directory,
                               "patched_test_" + str(index), spec.test_timeout)
            result["role"] = "regression" if index == 0 else "control"
            counts = result.get("counts", {})
            # Every mandatory test must actually run; skipped cases never pass this gate.
            result["matches_expectation"] = (result["exit_code"] == 0 and not result["timed_out"]
                and "junit_error" not in result and counts.get("passed", 0) > 0
                and all(counts.get(key) == 0 for key in ("failed", "errors", "skipped")))
            report["tests"].append(result)
            save()
            check_files()
            if not result["matches_expectation"]:
                raise ValueError("Mandatory patched test did not pass: " + target)
        report["stage"] = "final_integrity"
        save()
        for item in candidate["evidence"].values():
            if _sha(Path(item["path"]).read_bytes()) != item["sha256"]:
                raise ValueError("Original material changed during verification: " + item["path"])
        final_bytes = json.dumps(check_files(), ensure_ascii=False, indent=2).encode("utf-8")
        (directory / "final-files.json").write_bytes(final_bytes)
        report.update(status="independently_verified", stage="complete", independently_verified=True,
                      final_files_sha256=_sha(final_bytes), protected_files_unchanged=True,
                      patch_replay_only=True, model_repeatability_evaluated=False)
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="verification_failed", error_type=type(exc).__name__, error=str(exc))
        if isinstance(exc, KeyboardInterrupt):
            report["interrupted"] = True
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    save()
    write_summary(round_directory, spec.path_migrations)
    return report
