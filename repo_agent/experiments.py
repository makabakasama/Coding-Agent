"""Prepare and record a fresh G+R baseline without calling a model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
from typing import Callable
import uuid
import xml.etree.ElementTree as ET

from .workspace import create_worktree
from .path_migrations import recorded_path, recorded_environment
from .round_layout import allocate_round, workspace_name, write_summary


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    source: Path
    revision: str
    test_python: Path
    regression_target: str
    failure_contains: str
    control_targets: tuple[str, ...]
    import_module: str
    regression_patch: Path | None = None
    regression_sha256: str | None = None
    # Only non-sensitive settings; {run_dir} is expanded after allocating a run.
    environment: dict[str, str] = field(default_factory=dict)
    test_timeout: int = 90
    # Existing implementation files whose changes may form P; all others are protected.
    editable_paths: tuple[str, ...] = ()
    path_migrations: Path | None = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def experiment_environment(spec: ExperimentSpec, directory: Path, workspace: Path) -> dict[str, str]:
    """Map each stage's source imports to its own checkout, never an editable source clone."""
    return {key: value.replace("{run_dir}", str(directory)).replace("{workspace}", str(workspace))
            for key, value in spec.environment.items()}


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-c", "safe.directory=" + root.as_posix(), "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
         "-C", str(root), *args], input=data, capture_output=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def _files(root: Path) -> dict:
    """Fingerprint tracked and non-ignored files, including newly restored tests."""
    names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    result = {}
    for name in sorted(set(os.fsdecode(n) for n in names.split(b"\0") if n)):
        path = root / name
        # Git can check out these files while ordinary Win32 stat/open still
        # report them missing. Preserve the inventory's relative keys and hash
        # the actual files via the extended path namespace on Windows.
        if os.name == "nt" and len(str(path)) >= 260 and not str(path).startswith("\\\\?\\"):
            absolute = os.path.abspath(path)
            path = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
        if path.is_symlink():
            result[name] = {"symlink": os.readlink(path)}
        elif path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result[name] = {"sha256": digest}
        else:
            result[name] = {"missing": True}
    return result


def _capture(command: list[str], root: Path, environment: dict[str, str],
             directory: Path, label: str, timeout: int) -> dict:
    """Keep full stdout/stderr even on timeout; never invoke a shell."""
    timed_out = False
    with tempfile.TemporaryDirectory(prefix="repo-agent-prepare-cache-") as cache:
        actual = [command[0], "-X", "pycache_prefix=" + cache, *command[1:]]
        try:
            proc = subprocess.run(actual, cwd=root, env=environment, capture_output=True,
                                  timeout=timeout)
            code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, stdout, stderr = None, exc.stdout or b"", exc.stderr or b""
            timed_out = True
    (directory / (label + ".stdout.txt")).write_bytes(stdout)
    (directory / (label + ".stderr.txt")).write_bytes(stderr)
    return {"command": actual, "exit_code": code, "timed_out": timed_out,
            "stdout": label + ".stdout.txt", "stderr": label + ".stderr.txt"}


def _counts(path: Path) -> dict:
    counts = dict(passed=0, failed=0, errors=0, skipped=0)
    for case in ET.parse(path).getroot().iter("testcase"):
        kind = ("errors" if case.find("error") is not None else
                "failed" if case.find("failure") is not None else
                "skipped" if case.find("skipped") is not None else "passed")
        counts[kind] += 1
    return counts


def _run_test(python: Path, target: str, workspace: Path, environment: dict[str, str],
              directory: Path, label: str, timeout: int) -> dict:
    """Shared prepare/verify test capture, including absent or malformed JUnit."""
    from .tools import RepoTools
    if not target or target.startswith("-") or any(c in target for c in "\n\r\0"):
        raise ValueError("Invalid test target")
    RepoTools(workspace)._path(target.split("::", 1)[0])
    junit = directory / (label + ".xml")
    result = _capture([str(python), "-m", "pytest", "-q", "--maxfail=1",
                       "--junitxml=" + str(junit), target], workspace,
                      environment, directory, label, timeout)
    result.update(target=target, junit=junit.name)
    try:
        result["counts"] = _counts(junit)
        result["skipped_cases"] = [
            {"name": case.get("name"), "classname": case.get("classname"),
             "reason": case.find("skipped").get("message", "")}
            for case in ET.parse(junit).getroot().iter("testcase")
            if case.find("skipped") is not None
        ]
    except (OSError, ET.ParseError) as exc:
        result["junit_error"] = type(exc).__name__ + ": " + str(exc)
    return result


def _run_import_probe(python: Path, module: str, workspace: Path, environment: dict[str, str],
                      directory: Path, label: str, timeout: int) -> dict:
    code = (
        "import importlib,importlib.metadata as md,json,sys;"
        "m=importlib.import_module(sys.argv[1]);"
        "print('IMPORT_PROBE='+json.dumps({'python':sys.executable,'version':sys.version,"
        "'module_file':m.__file__,'packages':{d.metadata['Name']:d.version "
        "for d in md.distributions() if d.metadata['Name']}}))"
    )
    return _capture([str(python), "-c", code, module], workspace,
                    environment, directory, label, timeout)


def _read_provenance(probe: dict, directory: Path, workspace: Path, python: Path) -> dict:
    if probe["exit_code"] != 0 or probe["timed_out"]:
        raise ValueError("Target environment import failed; see import-probe logs")
    lines = (directory / probe["stdout"]).read_text(encoding="utf-8", errors="replace").splitlines()
    provenance = json.loads(next(line[len("IMPORT_PROBE="):] for line in lines
                                 if line.startswith("IMPORT_PROBE=")))
    imported = Path(provenance["module_file"]).resolve(strict=True)
    if not imported.is_relative_to(workspace) or Path(provenance["python"]).resolve() != python:
        raise ValueError("Import provenance does not match this workspace and interpreter")
    return provenance


def prepare_experiment(spec: ExperimentSpec, *, workspace_parent: Path,
                       trace_parent: Path, on_created: Callable[[dict], None] | None = None,
                       round_directory: Path | None = None, round_id: str | None = None,
                       record_subdir: str = "repair", workspace_role: str = "agent") -> dict:
    """Return a durable success/failure manifest; retain failed workspaces too."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", spec.name):
        raise ValueError("Invalid experiment name")
    if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*", spec.import_module):
        raise ValueError("Invalid import module")
    if not spec.failure_contains or not spec.control_targets or spec.test_timeout <= 0:
        raise ValueError("Expected failure, control targets and positive timeout are required")
    source = spec.source.resolve()
    python = spec.test_python.resolve()
    if round_directory is None:
        round_directory, round_id = allocate_round(trace_parent / spec.name, "prepare", spec.name)
    if round_id is None:
        raise ValueError("Round identifier is required")
    run_id = round_id
    directory = (round_directory / record_subdir).resolve()
    if not directory.is_relative_to(round_directory.resolve()) or directory == round_directory.resolve():
        raise ValueError("Preparation records must remain below the round")
    workspace = (workspace_parent / workspace_name(spec.name, run_id, workspace_role)).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    overrides = experiment_environment(spec, directory, workspace)
    environment = {**os.environ, **overrides}
    manifest = {
        "status": "preparing", "stage": "validate_inputs", "run_id": run_id,
        "name": spec.name, "source": str(source), "requested_revision": spec.revision,
        "workspace": str(workspace), "manifest": str(directory / "manifest.json"),
        "test_python": str(python), "platform": platform.platform(),
        "test_environment": overrides, "test_timeout": spec.test_timeout,
        "regression_target": spec.regression_target, "failure_contains": spec.failure_contains,
        "control_targets": list(spec.control_targets), "import_module": spec.import_module,
        "is_execution_sandbox": False, "model_started": False, "tests": [],
        "regression_patch": str(spec.regression_patch.resolve()) if spec.regression_patch else None,
        "regression_patch_sha256": None,
    }

    def save():
        target = directory / "manifest.json"
        temporary = directory / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    save()
    try:
        if on_created is not None:
            on_created(dict(manifest))
        if not source.is_dir() or not python.is_file():
            raise ValueError("Source directory and test interpreter are required")
        patch = spec.regression_patch.read_bytes() if spec.regression_patch else None
        if patch is not None:
            (directory / "regression.patch").write_bytes(patch)
            manifest["regression_patch_sha256"] = _sha(patch)
        if spec.regression_sha256 is not None and (patch is None or _sha(patch) != spec.regression_sha256):
            raise ValueError("Frozen regression patch checksum mismatch")
        manifest["stage"] = "create_worktree"
        save()
        created = create_worktree(source, workspace, spec.revision, git_config={
            "safe.directory": source.as_posix(), "core.autocrlf": "false", "core.longpaths": "true",
        })
        manifest["base_commit"] = created["base_commit"]
        manifest["stage"] = "restore_regression"
        save()
        if patch is not None:
            _git(workspace, "apply", "--check", "--whitespace=nowarn", "-", data=patch)
            _git(workspace, "apply", "--whitespace=nowarn", "-", data=patch)
        diff = _git(workspace, "diff", "--binary", "--no-ext-diff")
        (directory / "g-plus-r.patch").write_bytes(diff)
        manifest["g_plus_r_patch_sha256"] = _sha(diff)
        before = _files(workspace)
        (directory / "baseline-files.json").write_text(
            json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["baseline_files_sha256"] = _sha((directory / "baseline-files.json").read_bytes())
        manifest["stage"] = "import_provenance"
        save()
        probe = _run_import_probe(python, spec.import_module, workspace,
                                  environment, directory, "import-probe", spec.test_timeout)
        manifest["import_probe"] = probe
        save()
        provenance = _read_provenance(probe, directory, workspace, python)
        imported = Path(provenance["module_file"]).resolve(strict=True)
        manifest["provenance"] = provenance
        # R must not alter the implementation module under investigation.
        imported_relative = imported.relative_to(workspace).as_posix()
        if imported.read_bytes() != _git(workspace, "show", manifest["base_commit"] + ":" + imported_relative):
            raise ValueError("Regression preparation changed the imported implementation module")
        for index, target in enumerate((spec.regression_target, *spec.control_targets)):
            manifest["stage"] = "baseline_tests"
            save()
            label = "test_" + str(index)
            result = _run_test(python, target, workspace, environment, directory, label, spec.test_timeout)
            result["role"] = "regression" if index == 0 else "control"
            manifest["tests"].append(result)
            save()
            if result["timed_out"]:
                raise ValueError("Baseline test timed out: " + target)
            if "junit_error" in result:
                raise ValueError("Baseline JUnit unavailable: " + target)
            output = "\n".join((directory / result[k]).read_text(encoding="utf-8", errors="replace")
                               for k in ("stdout", "stderr"))
            counts = result["counts"]
            if index == 0:
                valid = (result["exit_code"] == 1 and counts["failed"] > 0
                         and counts["errors"] == 0 and spec.failure_contains in output)
            else:
                valid = (result["exit_code"] == 0 and counts["passed"] > 0
                         and counts["failed"] == counts["errors"] == 0)
            result["matches_expectation"] = valid
            save()
            if not valid:
                raise ValueError("Baseline test did not match its expected outcome: " + target)
        manifest["stage"] = "check_files"
        save()
        if _files(workspace) != before:
            raise ValueError("Repository files changed during import or baseline tests")
        manifest.update(status="baseline_verified", stage="complete", baseline_files_unchanged=True)
    except (Exception, KeyboardInterrupt) as exc:
        manifest.update(status="preparation_failed", error_type=type(exc).__name__, error=str(exc))
        if (isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired))
                and isinstance(exc.cmd, (list, tuple)) and exc.cmd
                and Path(exc.cmd[0]).stem.lower() == "git"):
            outputs = {}
            for stream in ("stdout", "stderr"):
                output = getattr(exc, stream, None) or b""
                data = output.encode("utf-8") if isinstance(output, str) else output
                filename = "git-failure." + stream + ".txt"
                (directory / filename).write_bytes(data)
                outputs[stream] = filename
                outputs[stream + "_sha256"] = _sha(data)
                outputs[stream + "_bytes"] = len(data)
            manifest["git_failure"] = {"command": [str(arg) for arg in exc.cmd],
                "exit_code": getattr(exc, "returncode", None),
                "timed_out": isinstance(exc, subprocess.TimeoutExpired), **outputs}
            reason = (directory / outputs["stderr"]).read_text(encoding="utf-8", errors="replace").strip()
            if reason:
                manifest["error"] += "\n" + reason[-2000:]
        save()
        if isinstance(exc, KeyboardInterrupt):
            write_summary(round_directory, spec.path_migrations)
            raise
    manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    save()
    write_summary(round_directory, spec.path_migrations)
    return manifest


def validate_baseline(manifest_path: Path, *, expected_spec: ExperimentSpec) -> dict:
    """Check the saved baseline against disk; do not import code or call a model.

    This detects accidental drift, not malicious edits to both data and hashes.
    Ignored files and installed dependency contents are outside this file check.
    """
    manifest_path = recorded_path(manifest_path, expected_spec.path_migrations).resolve(strict=True)
    directory = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Baseline manifest must be an object")
    required = ("manifest", "workspace", "base_commit", "source", "test_python",
                "baseline_files_sha256", "g_plus_r_patch_sha256", "provenance")
    if any(key not in manifest for key in required):
        raise ValueError("Incomplete baseline manifest; run --prepare again")
    if (manifest.get("status") != "baseline_verified" or manifest.get("stage") != "complete"
            or manifest.get("baseline_files_unchanged") is not True):
        raise ValueError("Baseline preparation was not verified")
    if recorded_path(manifest["manifest"], expected_spec.path_migrations) != manifest_path:
        raise ValueError("Manifest location differs from its recorded location")
    if (directory / "agent-run.json").exists():
        raise ValueError("This baseline already has an Agent run; create a fresh one with --prepare")
    workspace = recorded_path(manifest["workspace"], expected_spec.path_migrations).resolve(strict=True)
    source = expected_spec.source.resolve(strict=True)
    python = expected_spec.test_python.resolve(strict=True)
    expected_environment = experiment_environment(expected_spec, directory, workspace)
    expected_fields = {
        "name": expected_spec.name, "source": str(source), "test_python": str(python),
        "regression_target": expected_spec.regression_target,
        "control_targets": list(expected_spec.control_targets),
        "failure_contains": expected_spec.failure_contains,
        "import_module": expected_spec.import_module,
        "test_timeout": expected_spec.test_timeout, "test_environment": expected_environment,
    }
    for key, value in expected_fields.items():
        actual = manifest.get(key)
        if key in ("source", "test_python"):
            actual = str(recorded_path(actual, expected_spec.path_migrations))
        elif key == "test_environment":
            actual = recorded_environment(actual, expected_spec.path_migrations)
            value = recorded_environment(value, expected_spec.path_migrations)
        if actual != value:
            raise ValueError("Baseline configuration differs from this experiment: " + key)
    base = _git(source, "rev-parse", "--verify", expected_spec.revision + "^{commit}").decode().strip()
    if manifest["base_commit"] != base or _git(workspace, "rev-parse", "HEAD").decode().strip() != base:
        raise ValueError("Workspace HEAD differs from the fixed baseline commit")
    if Path(os.fsdecode(_git(workspace, "rev-parse", "--show-toplevel")).strip()).resolve() != workspace:
        raise ValueError("Workspace is not the root of the recorded checkout")
    if _git(workspace, "diff", "--cached", "--name-only").strip():
        raise ValueError("Workspace index contains staged changes")
    if not python.is_file():
        raise ValueError("Recorded test interpreter is missing")
    provenance = manifest["provenance"]
    if (not isinstance(provenance, dict)
            or recorded_path(provenance.get("python", ""), expected_spec.path_migrations) != python
            or not recorded_path(provenance.get("module_file", ""), expected_spec.path_migrations).is_relative_to(workspace)):
        raise ValueError("Recorded import provenance does not match the baseline")
    artifacts = {"baseline-files.json": manifest["baseline_files_sha256"],
                 "g-plus-r.patch": manifest["g_plus_r_patch_sha256"]}
    if expected_spec.regression_patch is not None:
        expected_r = expected_spec.regression_sha256 or _sha(expected_spec.regression_patch.read_bytes())
        if manifest.get("regression_patch_sha256") != expected_r:
            raise ValueError("Regression patch differs from the frozen experiment input")
        artifacts["regression.patch"] = expected_r
    elif manifest.get("regression_patch_sha256") is not None:
        raise ValueError("Unexpected regression patch in this baseline")
    for name, digest in artifacts.items():
        if _sha((directory / name).read_bytes()) != digest:
            raise ValueError("Saved baseline artifact changed: " + name)
    before = json.loads((directory / "baseline-files.json").read_text(encoding="utf-8"))
    current = _files(workspace)
    if not isinstance(before, dict) or not before:
        raise ValueError("Saved baseline file inventory is invalid")
    if current != before:
        changed = sorted(name for name in before.keys() | current.keys()
                         if before.get(name) != current.get(name))
        raise ValueError("Workspace differs from the verified G+R baseline: " + ", ".join(changed[:8]))
    tests = manifest.get("tests", [])
    targets = [expected_spec.regression_target, *expected_spec.control_targets]
    if (not isinstance(tests, list) or len(tests) != len(targets)
            or any(not isinstance(test, dict) or test.get("target") != target
                   or test.get("matches_expectation") is not True
                   for test, target in zip(tests, targets))):
        raise ValueError("Baseline test evidence is incomplete")
    if not expected_spec.editable_paths or len(set(expected_spec.editable_paths)) != len(expected_spec.editable_paths):
        raise ValueError("Declare the existing implementation files eligible for P")
    for name in expected_spec.editable_paths:
        path = workspace / name
        if (Path(name).is_absolute() or ".." in Path(name).parts
                or Path(name).as_posix() != name or not path.resolve().is_relative_to(workspace)
                or not isinstance(before.get(name), dict) or "sha256" not in before[name]):
            raise ValueError("Editable path must be an existing baseline file: " + name)
        # Exporting a diff against G is safe only when R did not change this file.
        if _sha(_git(workspace, "show", base + ":" + name)) != before[name]["sha256"]:
            raise ValueError("R overlaps an editable implementation file: " + name)
    return manifest


def claim_baseline(manifest_path: Path, *, expected_spec: ExperimentSpec) -> dict:
    """Atomically claim one run; a second caller cannot reuse the same baseline."""
    manifest_path = recorded_path(manifest_path, expected_spec.path_migrations).resolve(strict=True)
    manifest = validate_baseline(manifest_path, expected_spec=expected_spec)
    directory = manifest_path.resolve().parent
    receipt = {
        "status": "claimed", "manifest": str(manifest_path.resolve()),
        "manifest_sha256": _sha(manifest_path.read_bytes()), "workspace": manifest["workspace"],
        "claimed_at_utc": datetime.now(timezone.utc).isoformat(), "model_started": False,
        "editable_paths": list(expected_spec.editable_paths),
    }
    try:
        with (directory / "agent-run.json").open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
    except FileExistsError as exc:
        raise ValueError("This baseline was claimed by another run; use --prepare") from exc
    return manifest
