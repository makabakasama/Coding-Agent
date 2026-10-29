"""pydantic-ai #10525 known-answer experiment. --run/--manifest call a REAL MODEL.

--prepare and --check-manifest do not call a model; --verify only replays saved P.
The target repository/environment must first be created by the user (experiment-02 runbook).
Records share one Beijing-time round ID; see runs/pydantic-ai-10525/INDEX.md.
"""
from __future__ import annotations

import os
from pathlib import Path

from repo_agent.experiment_runner import ExperimentRunner
from repo_agent.experiments import ExperimentSpec


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_COMMIT = "b4b9ad3de2d7a9cbfd7c70715f1f945496e990ce"
REGRESSION_SHA256 = "4365ee7b3842d66dda5743dcae4d2e71282f5132455561a730be69c58ab28a20"
TEST_MODULE = "tests/test_messages.py::"
REGRESSION_TARGET = TEST_MODULE + "test_issue_10525_non_object_json_optimized"
CONTROL_TARGETS = (
    TEST_MODULE + "test_issue_10525_non_object_json_normal",
    TEST_MODULE + "test_args_as_dict_valid_json",
    TEST_MODULE + "test_args_as_dict_non_dict_json_returns_invalid_json_wrapper",
    TEST_MODULE + "test_args_as_dict_raise_if_invalid_non_dict_json",
)


def experiment_spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="pydantic-ai-10525", source=PROJECT_ROOT / "experiments/pydantic-ai/inputs/source",
        path_migrations=PROJECT_ROOT / "experiments/path-migrations.json",
        revision=BASE_COMMIT,
        test_python=PROJECT_ROOT / "experiments/pydantic-ai/inputs/env/Scripts/python.exe",
        regression_patch=PROJECT_ROOT / "examples/fixtures/pydantic_ai_10525/regression.patch",
        regression_sha256=REGRESSION_SHA256,
        regression_target=REGRESSION_TARGET,
        failure_contains="PYDANTIC_AI_10525_BEHAVIOR_MISMATCH",
        control_targets=CONTROL_TARGETS, import_module="pydantic_ai.messages",
        environment={"PYTHONPATH": os.pathsep.join(("{workspace}/pydantic_ai_slim", "{workspace}/pydantic_graph"))},
        editable_paths=("pydantic_ai_slim/pydantic_ai/messages.py",), test_timeout=180,
    )


TASK = """Repair the message-model handling of non-object JSON tool arguments.

ToolCallPart(tool_name='example', args='[]').args_as_dict() should return
{'INVALID_JSON': '[]'} by default, and LoadCapabilityCallPart(args='[]').capability_id
should return None. This contract must hold in both normal Python and Python -O.
Lists, scalars, and null must not escape as dictionary values or crash capability lookup.
Preserve existing normal-mode behavior, including strict invalid-argument exceptions.

Inspect the repository, make the smallest justified implementation edit, and use actual
test feedback. Do not edit tests, test helpers, or configuration. This is a historical
known-answer evaluation of issue #10525; do not submit a PR.

Run the frozen optimized regression and the normal-mode controls individually:
""" + "\n".join((REGRESSION_TARGET, *CONTROL_TARGETS))


def _runner() -> ExperimentRunner:
    return ExperimentRunner(PROJECT_ROOT, TASK, max_steps=20,
        workspace_parent=PROJECT_ROOT / "experiments/pydantic-ai/workspaces")


def main(argv: list[str] | None = None):
    return _runner().main(argv, spec_factory=experiment_spec, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
