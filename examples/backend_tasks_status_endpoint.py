"""First real V0.5 task: implement a task status endpoint in a local FastAPI project.

--prepare / --check-manifest do not call a model. --run starts a REAL MODEL and
automatically prepares G+R, runs the Agent, exports P and independently verifies
it on a fresh worktree. --verify only replays a saved P, without a model.

This is NOT a known-answer experiment: the required endpoint does not exist yet.
The frozen acceptance tests in examples/fixtures/backend-tasks-status-endpoint/
are authoritative and must not be changed by the Agent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from repo_agent.core.agent import Agent
from repo_agent.core.provider import OpenAICompatibleProvider
from repo_agent.experimentation.experiments import ExperimentSpec, prepare_experiment
from repo_agent.experimentation.experiment_artifacts import export_run_artifacts
from repo_agent.experimentation.experiment_runner import ExperimentRunner
from repo_agent.experimentation.experiment_verify import verify_experiment
from repo_agent.infrastructure.run_evidence import archive_agent_source

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_REVISION = "ffe361fc1d5742754df83320349d992febb19ea1"
REGRESSION_SHA256 = "662cc70b89d4241a4f283d9ce1d419837ebc1425605c2485a8db9a517358588a"
REGRESSION_TARGET = "demo_projects/test/test_status_endpoint.py"
CONTROL_TARGETS = ("demo_projects/test/test_health.py::test_health",)
MAX_STEPS = 20


def experiment_spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="backend-tasks-status-endpoint",
        source=PROJECT_ROOT / "experiments/backend-tasks/inputs/source",
        revision=BASE_REVISION,
        test_python=PROJECT_ROOT / "experiments/backend-tasks/inputs/env/Scripts/python.exe",
        regression_patch=PROJECT_ROOT
        / "examples/fixtures/backend-tasks-status-endpoint/regression.patch",
        regression_sha256=REGRESSION_SHA256,
        regression_target=REGRESSION_TARGET,
        failure_contains="405",
        control_targets=CONTROL_TARGETS,
        import_module="demo_projects.app.service",
        editable_paths=(
            "demo_projects/app/schema.py",
            "demo_projects/app/service.py",
            "demo_projects/app/router.py",
        ),
        test_timeout=120,
    )


TASK = """Implement a task status update endpoint for the FastAPI service in
demo_projects/. The endpoint does not exist yet; this is a real feature request,
not a known-answer bug fix.

Required contract:

  PATCH /tasks/{task_id}
  Header: X-API-Key (the existing router-level authentication)
  Body:   {"status": "pending" | "running" | "completed" | "failed"}

Expected behaviour:
  1. A valid update returns HTTP 200 and the updated TaskRead
     (id, goal, priority, status).
  2. A status outside the four allowed values is rejected with HTTP 422 before
     any repository call.
  3. An unknown task id returns HTTP 404 with {"detail": "Task not found"}.
  4. The existing GET /tasks/{task_id} caches the task under "task:{id}" for 30
     seconds. After a successful PATCH, that cache entry MUST be invalidated or
     refreshed, so a subsequent GET returns the NEW status, not a stale one.
  5. If the cache backend raises redis.exceptions.RedisError while invalidating,
     the status update must still succeed (degrade gracefully, like the existing
     get_task does).

Existing pieces to reuse, not reinvent:
  - TaskRepository.update_status(task_id, status) already exists and returns the
    updated row or None.
  - TaskService.__init__(repository, redis_client) must keep its signature.
  - The router already applies require_api_key at the /tasks router level.

Allowed to edit ONLY:
  demo_projects/app/schema.py
  demo_projects/app/service.py
  demo_projects/app/router.py

Do NOT edit any test file, the frozen acceptance tests, configuration, worker.py,
app/main.py, app/repository.py, app/dependencies.py, app/security.py, or
requirements.txt. Do not change the worker's status values or the database.

Use real pytest feedback. The frozen tests are at
demo_projects/test/test_status_endpoint.py. Do not assume a model summary or a
single passing check means the request is complete; all six tests in that file
must pass, including the stale-cache and cache-failure cases.

Report the actual change, the tests you ran and their results, and any remaining
limitations. Do not submit a PR or modify anything outside this repository.
"""


def _runner() -> ExperimentRunner:
    return ExperimentRunner(
        PROJECT_ROOT,
        TASK,
        MAX_STEPS,
        model_factory=OpenAICompatibleProvider,
        agent_factory=Agent,
        prepare=prepare_experiment,
        verify=verify_experiment,
        export_artifacts=export_run_artifacts,
        archive_source=archive_agent_source,
        workspace_parent=PROJECT_ROOT / "experiments/backend-tasks/workspaces",
    )


def run_prepared(path: Path, *, spec: ExperimentSpec,
                 on_stage: Callable[..., None] | None = None) -> tuple[dict, int]:
    return _runner().run_prepared(path, spec=spec, on_stage=on_stage)


def run_experiment(spec: ExperimentSpec, *, on_allocated=None) -> dict:
    return _runner().run_experiment(spec, on_allocated=on_allocated)


def main(argv: list[str] | None = None):
    return _runner().main(argv, spec_factory=experiment_spec, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
