"""Shared fixed-G/R/P experiment orchestration; examples provide explicit configuration."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Callable
from urllib.parse import urlsplit
import uuid

from .agent import Agent
from .provider import OpenAICompatibleProvider
from .tools import RepoTools
from .experiments import ExperimentSpec, claim_baseline, prepare_experiment, validate_baseline
from .experiment_artifacts import export_run_artifacts
from .experiment_verify import verify_experiment
from .run_evidence import archive_agent_source, check_agent_source, source_archive_summary
from .path_migrations import recorded_path, recorded_environment
from .round_layout import allocate_round, find_round, write_summary

def _save_json(path: Path, value: dict):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)



@dataclass(frozen=True)
class ExperimentRunner:
    project_root: Path
    task: str
    max_steps: int = 20
    model_factory: Callable = OpenAICompatibleProvider
    agent_factory: Callable = Agent
    prepare: Callable = prepare_experiment
    verify: Callable = verify_experiment
    export_artifacts: Callable = export_run_artifacts
    archive_source: Callable = archive_agent_source
    workspace_parent: Path | None = None

    def _workspace_parent(self) -> Path:
        return self.workspace_parent if self.workspace_parent is not None else self.project_root / "experiments"

    def run_prepared(self, path: Path, *, spec: ExperimentSpec,
                     on_stage: Callable[..., None] | None = None) -> tuple[dict, int]:
        """Claim, run and export once; shared by --manifest and --run."""
        path = recorded_path(path, spec.path_migrations)
        try:
            baseline = claim_baseline(path, expected_spec=spec)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
            return {"status": "startup_refused", "reason": str(exc), "model_started": False}, 1
        receipt_path = path.resolve().parent / "agent-run.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if spec.path_migrations is not None and spec.path_migrations.exists():
            receipt["path_migrations"] = str(spec.path_migrations.resolve())

        def save_receipt():
            _save_json(receipt_path, receipt)

        tools = None
        source_manifest = receipt_path.parent / "agent-source/manifest.json"
        receipt["agent_source"] = {"status": "capturing", "manifest": str(source_manifest)}
        receipt["test_logs"] = {"directory": str(receipt_path.parent / "test-logs"), "calls": []}
        save_receipt()
        try:
            self.archive_source(source_manifest.parent)
            receipt["agent_source"] = source_archive_summary(source_manifest)
            save_receipt()
            if receipt["agent_source"]["status"] != "captured":
                raise ValueError("Agent disk-source archive did not complete; model not started")
            tools = RepoTools(recorded_path(baseline["workspace"], spec.path_migrations), allow_writes=True, allow_tests=True,
                              test_python=recorded_path(baseline["test_python"], spec.path_migrations), test_timeout=baseline["test_timeout"],
                              test_env=recorded_environment(baseline["test_environment"], spec.path_migrations), test_log_dir=receipt_path.parent / "test-logs")
            model = self.model_factory()
            agent = self.agent_factory(model, tools, trace_dir=receipt_path.parent / "agent", max_steps=self.max_steps)
            receipt.update(status="running", task=self.task, max_steps=self.max_steps,
                           decision_protocol="native_tool_calls", trace=str(agent.trace_path),
                           model={"name": model.model, "profile": model.profile,
                                  "api_host": urlsplit(model.base_url).hostname})
            save_receipt()
            receipt["model_started"] = True
            save_receipt()
            if on_stage is not None:
                on_stage("running_agent", agent_status="running", model_started=True)
            result = agent.run(self.task)
            receipt.update(status=result["status"], result=result)
            exit_code = 130 if result["status"] == "interrupted" else (1 if result["status"] == "error" else 0)
        except (Exception, KeyboardInterrupt) as exc:
            receipt.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "startup_or_run_error",
                           error_type=type(exc).__name__, error=str(exc))
            result = {"status": receipt["status"], "error_type": type(exc).__name__, "record": str(receipt_path)}
            exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        receipt["result"] = result
        receipt["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        if tools is not None:
            receipt["test_logs"]["calls"] = tools.test_log_records
        try:
            if receipt["agent_source"]["status"] == "capturing" and source_manifest.is_file():
                # Also recover the archive status if capture was interrupted.
                receipt["agent_source"] = source_archive_summary(source_manifest)
            elif receipt["agent_source"]["status"] == "capturing":
                receipt["agent_source"]["status"] = "capture_interrupted" if exit_code == 130 else "capture_failed"
            if receipt["agent_source"]["status"] == "captured":
                receipt["agent_source_check"] = check_agent_source(receipt["agent_source"])
            else:
                receipt["agent_source_check"] = {"status": "not_checked"}
        except KeyboardInterrupt:
            receipt["agent_source_check"] = {"status": "interrupted"}
            result.update(interrupted=True, interruption_stage="source_check")
            exit_code = 130
        except Exception as exc:
            receipt["agent_source_check"] = {"status": "check_failed", "error_type": type(exc).__name__}
        receipt["evidence_status"] = (
            "incomplete" if receipt["agent_source"].get("status") != "captured"
                or receipt["agent_source_check"]["status"] not in ("unchanged", "source_drift")
                or any(call["status"] != "recorded" for call in receipt["test_logs"]["calls"])
            else ("source_drift" if receipt["agent_source_check"]["status"] == "source_drift" else "recorded"))
        save_receipt()
        if on_stage is not None:
            on_stage("exporting_candidate", agent_status=receipt["status"], model_started=receipt["model_started"])
        # Export even after an exception or step limit, preserving the Agent's original status.
        try:
            report = self.export_artifacts(baseline, receipt)
            receipt["artifacts"] = {key: report[key] for key in
                                    ("status", "report", "eligible_for_verification", "independently_verified")}
            if report["status"] not in ("candidate_exported", "no_code_change"):
                exit_code = exit_code or 1
        except (Exception, KeyboardInterrupt) as exc:
            interrupted = isinstance(exc, KeyboardInterrupt)
            receipt["artifacts"] = {"status": "export_interrupted" if interrupted else "export_failed",
                                    "error_type": type(exc).__name__,
                                    "error": str(exc), "eligible_for_verification": False}
            if interrupted:
                receipt.update(interrupted=True, interruption_stage="export")
                result.update(interrupted=True, interruption_stage="export")
            exit_code = 130 if interrupted else (exit_code or 1)
        result["record"] = str(receipt_path)
        result["artifacts"] = receipt["artifacts"]
        result["model_started"] = receipt["model_started"]
        for key in ("agent_source", "agent_source_check", "test_logs", "evidence_status"):
            result[key] = receipt[key]
        save_receipt()
        round_directory = find_round(path)
        if round_directory is not None:
            write_summary(round_directory, spec.path_migrations)
        return result, exit_code


    def run_experiment(self, spec: ExperimentSpec) -> dict:
        """One invocation, new G+R and independent P verification; retain every stage."""
        directory, identifier = allocate_round(self.project_root / "runs" / spec.name, "run", spec.name)
        path = directory / "experiment.json"
        report = {"report": str(path), "run_id": identifier, "status": "running", "stage": "preparing",
                  "prepare_status": "preparing", "agent_status": "not_started", "model_started": False,
                  "export_status": "not_started", "verification_status": "not_started",
                  "independently_verified": False, "exit_code": 1,
                  "started_at_utc": datetime.now(timezone.utc).isoformat()}

        def update(stage: str, **fields):
            report.update(stage=stage, **fields)
            _save_json(path, report)

        def skip(reason: str):
            report.update(verification_status="skipped", verification_skip_reason=reason)

        def created(baseline: dict):
            update("preparing", prepare_manifest=baseline["manifest"], workspace=baseline["workspace"],
                   agent_record=str(Path(baseline["manifest"]).parent / "agent-run.json"))

        update("preparing")
        try:
            baseline = self.prepare(spec, workspace_parent=self._workspace_parent(),
                                          trace_parent=self.project_root / "runs", on_created=created,
                                          round_directory=directory, round_id=identifier)
            update("preparing", prepare_status=baseline["status"], prepare_manifest=baseline["manifest"],
                   workspace=baseline["workspace"],
                   agent_record=str(Path(baseline["manifest"]).parent / "agent-run.json"))
            if baseline["status"] != "baseline_verified":
                skip("preparation_failed")
                report.update(status="failed", prepare_error=baseline.get("error"))
            else:
                update("starting_agent")
                result, code = self.run_prepared(Path(baseline["manifest"]), spec=spec, on_stage=update)
                artifacts = result.get("artifacts", {})
                report.update(agent_status=result["status"], agent_result=result, agent_exit_code=code,
                              model_started=result.get("model_started", False),
                              export_status=artifacts.get("status", "not_exported"),
                              candidate_report=artifacts.get("report"))
                for key in ("agent_source", "agent_source_check", "test_logs", "evidence_status"):
                    if key in result:
                        report[key] = result[key]
                update("selecting_verification")
                if code == 130 or result["status"] == "interrupted" or result.get("interrupted") is True:
                    skip("user_interrupted")
                    report.update(status="interrupted", exit_code=130)
                elif (artifacts.get("status") != "candidate_exported"
                      or artifacts.get("eligible_for_verification") is not True):
                    reason = (artifacts.get("status", "not_exported") if artifacts.get("status") != "candidate_exported"
                              else "eligible_for_verification_not_true")
                    skip("candidate_not_eligible: " + reason)
                    report["status"] = "failed"
                else:
                    update("verifying", verification_status="running")
                    verification = self.verify(Path(baseline["manifest"]), expected_spec=spec,
                        workspace_parent=self._workspace_parent(), trace_parent=self.project_root / "runs")
                    report.update(verification_report=verification["report"],
                                  verification_workspace=verification.get("workspace"),
                                  verification_status=verification["status"],
                                  verification_result={key: verification[key] for key in
                                      ("status", "stage", "independently_verified", "interrupted", "error", "error_type")
                                      if key in verification})
                    if verification.get("interrupted") is True:
                        report.update(status="interrupted", verification_status="interrupted", exit_code=130)
                    elif (verification["status"] == "independently_verified"
                          and verification.get("independently_verified") is True):
                        report.update(status="independently_verified", stage="complete",
                                      independently_verified=True, exit_code=0)
                    else:
                        report["status"] = "failed"
        except KeyboardInterrupt:
            # Never start another stage after a user's stop request.
            if report["stage"] == "verifying":
                report["verification_status"] = "interrupted"
            else:
                skip("user_interrupted")
            if report["prepare_status"] == "preparing":
                report["prepare_status"] = "interrupted"
            report.update(status="interrupted", exit_code=130)
        except Exception as exc:
            if report["prepare_status"] == "preparing":
                report["prepare_status"] = "preparation_failed"
            if report["stage"] == "verifying":
                report["verification_status"] = "verification_failed"
            else:
                skip("stage_failed: " + report["stage"])
            report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        _save_json(path, report)
        write_summary(directory, spec.path_migrations)
        return report


    def main(self, argv: list[str] | None = None, *, spec_factory: Callable[[], ExperimentSpec], description: str):
        parser = argparse.ArgumentParser(description=description)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--run", action="store_true",
                          help="REAL MODEL: prepare, repair, export, independently verify and save one final report")
        mode.add_argument("--prepare", action="store_true",
                          help="Create and verify a fresh G+R baseline without calling a model")
        mode.add_argument("--manifest", type=Path,
                          help="Validate and claim a prepared baseline, then start the real model")
        mode.add_argument("--check-manifest", type=Path,
                          help="Check the baseline files and metadata without calling a model")
        mode.add_argument("--verify", type=Path,
                          help="Independently verify a completed run's saved P without calling a model")
        args = parser.parse_args(argv)
        spec = spec_factory()
        if args.run:
            result = self.run_experiment(spec)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result["exit_code"]
        if args.verify:
            result = self.verify(args.verify, expected_spec=spec,
                                       workspace_parent=self._workspace_parent(),
                                       trace_parent=self.project_root / "runs")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 130 if result.get("interrupted") else (0 if result["independently_verified"] else 1)
        if args.prepare:
            result = self.prepare(spec, workspace_parent=self._workspace_parent(),
                                        trace_parent=self.project_root / "runs")
            summary = {key: result[key] for key in ("status", "workspace", "manifest")}
            summary["tests"] = [{key: test[key] for key in ("role", "target", "exit_code", "counts", "matches_expectation")
                                 if key in test} for test in result["tests"]]
            if "error" in result:
                summary.update(stage=result["stage"], error=result["error"])
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "baseline_verified" else 1
        if args.check_manifest:
            try:
                baseline = validate_baseline(args.check_manifest, expected_spec=spec)
                result = {"status": "baseline_check_passed", "workspace": baseline["workspace"], "model_started": False}
                code = 0
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
                result = {"status": "startup_refused", "reason": str(exc), "model_started": False}
                code = 1
        else:
            result, code = self.run_prepared(args.manifest, spec=spec)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code

