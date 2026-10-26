"""Prepare a browser-use #5998 baseline or run the model in a selected worktree.

--prepare creates a fresh G+R baseline and runs its tests without calling a model.
--manifest validates and claims a prepared baseline before enabling model edits.
--check-manifest only checks a baseline; model settings come from the existing .env.
--verify independently replays a saved candidate on a fresh checkout without a model.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from repo_agent.agent import Agent
from repo_agent.provider import OpenAICompatibleProvider
from repo_agent.tools import RepoTools
from repo_agent.experiments import ExperimentSpec, claim_baseline, prepare_experiment, validate_baseline
from repo_agent.experiment_artifacts import export_run_artifacts
from repo_agent.experiment_verify import verify_experiment


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_PYTHON = PROJECT_ROOT / "experiments" / "browser-use-env" / "Scripts" / "python.exe"
MAX_STEPS = 20


def experiment_spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="browser-use-5998", source=PROJECT_ROOT / "experiments/browser-use-source",
        revision="7be96ed8bafa8dfe1eef228b59cf5c884b8b2431", test_python=TEST_PYTHON,
        regression_patch=PROJECT_ROOT / "examples/fixtures/browser_use_5998/regression.patch",
        regression_sha256="3739b170ece5bc66da973846898e492115c0f45e24b46d851b8a2b37a8fc176e",
        regression_target="tests/ci/infrastructure/test_registry_core.py::TestActionRegistryParameterPatterns::test_keyword_only_parameters_no_browser",
        failure_contains="takes 1 positional argument but 2 were given",
        control_targets=("tests/ci/infrastructure/test_registry_core.py::TestActionRegistryParameterPatterns::test_individual_parameters_no_browser",),
        import_module="browser_use.tools.registry.service",
        environment={"ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CONFIG_DIR": "{run_dir}/browseruse-config"},
        editable_paths=("browser_use/tools/registry/service.py",),
    )

TASK = """Fix the action registry's handling of keyword-only parameters.

A registered action with this signature:
async def action(text: str, *, number: int = 10)
should accept {"text": "hello", "number": 42} through execute_action.
Currently, execution raises a positional-argument TypeError.

Inspect the repository and make a minimal implementation change.
Do not modify tests or project configuration.
This is a known-answer experiment for issue #5998; do not submit a PR.

Run both tests individually:
tests/ci/infrastructure/test_registry_core.py::TestActionRegistryParameterPatterns::test_keyword_only_parameters_no_browser
tests/ci/infrastructure/test_registry_core.py::TestActionRegistryParameterPatterns::test_individual_parameters_no_browser

Use actual test feedback to guide further repairs.
Report the changes, test results, and any remaining limitations.
"""


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true",
                      help="Create and verify a fresh G+R baseline without calling a model")
    mode.add_argument("--manifest", type=Path,
                      help="Validate and claim a prepared baseline, then start the real model")
    mode.add_argument("--check-manifest", type=Path,
                      help="Check the baseline files and metadata without calling a model")
    mode.add_argument("--verify", type=Path,
                      help="Independently verify a completed run's saved P without calling a model")
    args = parser.parse_args(argv)
    spec = experiment_spec()
    if args.verify:
        result = verify_experiment(args.verify, expected_spec=spec,
                                   workspace_parent=PROJECT_ROOT / "experiments",
                                   trace_parent=PROJECT_ROOT / "runs")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["independently_verified"] else 1
    if args.prepare:
        result = prepare_experiment(spec, workspace_parent=PROJECT_ROOT / "experiments",
                                    trace_parent=PROJECT_ROOT / "runs")
        summary = {key: result[key] for key in ("status", "workspace", "manifest")}
        summary["tests"] = [{key: test[key] for key in ("role", "target", "exit_code", "counts", "matches_expectation")
                             if key in test} for test in result["tests"]]
        if "error" in result:
            summary.update(stage=result["stage"], error=result["error"])
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "baseline_verified" else 1
    path = args.manifest or args.check_manifest
    try:
        if args.check_manifest:
            baseline = validate_baseline(path, expected_spec=spec)
            print(json.dumps({"status": "baseline_check_passed", "workspace": baseline["workspace"],
                              "model_started": False}, ensure_ascii=False, indent=2))
            return 0
        baseline = claim_baseline(path, expected_spec=spec)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "startup_refused", "reason": str(exc), "model_started": False},
                         ensure_ascii=False, indent=2))
        return 1
    receipt_path = path.resolve().parent / "agent-run.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    def save_receipt():
        temporary = receipt_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(receipt_path)

    try:
        tools = RepoTools(Path(baseline["workspace"]), allow_writes=True, allow_tests=True,
                          test_python=Path(baseline["test_python"]), test_timeout=baseline["test_timeout"],
                          test_env=baseline["test_environment"])
        model = OpenAICompatibleProvider()
        agent = Agent(model, tools, trace_dir=receipt_path.parent / "agent", max_steps=MAX_STEPS)
        receipt.update(status="running", task=TASK, max_steps=MAX_STEPS,
                       decision_protocol="native_tool_calls", trace=str(agent.trace_path),
                       model={"name": model.model, "profile": model.profile,
                              "api_host": urlsplit(model.base_url).hostname})
        save_receipt()
        receipt["model_started"] = True
        save_receipt()
        result = agent.run(TASK)
        receipt.update(status=result["status"], result=result)
        exit_code = 1 if result["status"] == "error" else 0
    except (Exception, KeyboardInterrupt) as exc:
        receipt.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "startup_or_run_error",
                       error_type=type(exc).__name__, error=str(exc))
        result = {"status": receipt["status"], "error_type": type(exc).__name__, "record": str(receipt_path)}
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    receipt["result"] = result
    receipt["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    save_receipt()
    # Export even after an exception or step limit, preserving the Agent's original status.
    try:
        report = export_run_artifacts(baseline, receipt)
        receipt["artifacts"] = {key: report[key] for key in
                                ("status", "report", "eligible_for_verification", "independently_verified")}
        if report["status"] not in ("candidate_exported", "no_code_change"):
            exit_code = exit_code or 1
    except Exception as exc:
        receipt["artifacts"] = {"status": "export_failed", "error_type": type(exc).__name__,
                                "error": str(exc), "eligible_for_verification": False}
        exit_code = exit_code or 1
    result["record"] = str(receipt_path)
    result["artifacts"] = receipt["artifacts"]
    save_receipt()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
