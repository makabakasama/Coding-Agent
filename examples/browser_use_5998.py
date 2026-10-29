"""Prepare a browser-use #5998 baseline or run the model in a selected worktree.

--prepare creates a fresh G+R baseline and runs its tests without calling a model.
--manifest validates and claims a prepared baseline before enabling model edits.
--check-manifest only checks a baseline; model settings come from the existing .env.
--verify independently replays a saved candidate on a fresh checkout without a model.
--run starts a REAL MODEL and automatically prepares, repairs, exports and verifies.
Records share one Beijing-time round ID; see runs/browser-use-5998/INDEX.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from repo_agent.agent import Agent
from repo_agent.provider import OpenAICompatibleProvider
from repo_agent.tools import RepoTools
from repo_agent.experiments import ExperimentSpec, claim_baseline, prepare_experiment, validate_baseline
from repo_agent.experiment_artifacts import export_run_artifacts
from repo_agent.experiment_verify import verify_experiment
from repo_agent.run_evidence import archive_agent_source, check_agent_source, source_archive_summary
from repo_agent.experiment_runner import ExperimentRunner


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_PYTHON = PROJECT_ROOT / "experiments/browser-use/inputs/env/Scripts/python.exe"
MAX_STEPS = 20


def experiment_spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="browser-use-5998", source=PROJECT_ROOT / "experiments/browser-use/inputs/source",
        path_migrations=PROJECT_ROOT / "experiments/path-migrations.json",
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


def _runner() -> ExperimentRunner:
    return ExperimentRunner(PROJECT_ROOT, TASK, MAX_STEPS,
        model_factory=OpenAICompatibleProvider, agent_factory=Agent,
        prepare=prepare_experiment, verify=verify_experiment,
        export_artifacts=export_run_artifacts, archive_source=archive_agent_source,
        workspace_parent=PROJECT_ROOT / "experiments/browser-use/workspaces")


def run_prepared(path: Path, *, spec: ExperimentSpec,
                 on_stage: Callable[..., None] | None = None) -> tuple[dict, int]:
    return _runner().run_prepared(path, spec=spec, on_stage=on_stage)


def run_experiment(spec: ExperimentSpec) -> dict:
    return _runner().run_experiment(spec)


def main(argv: list[str] | None = None):
    return _runner().main(argv, spec_factory=experiment_spec, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
