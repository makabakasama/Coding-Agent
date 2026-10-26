"""Small observe-decide-act loop with durable local JSONL trace."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Protocol
import uuid

from .prompts import SYSTEM_PROMPT
from .decisions import Decision, DecisionError
from .tools import RepoTools


class Model(Protocol):
    def decide(self, messages: list[dict]) -> Decision: ...


class Agent:
    def __init__(self, model: Model, tools: RepoTools, *, trace_dir: Path,
                 max_steps: int = 12):
        if not 1 <= max_steps <= 50:
            raise ValueError("max_steps must be in 1..50")
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        trace_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = trace_dir / f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}.jsonl"

    def _record(self, item: dict):
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def run(self, task: str) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Coding task:\n" + task + "\nRepository is already checked out locally."},
        ]
        self._record({"type": "start", "task": task, "repo": str(self.tools.root),
                      "decision_protocol": "native_tool_calls"})
        last_test_passed = None
        applied_patches = 0
        actions = 0
        for step in range(1, self.max_steps + 1):
            try:
                decision = self.model.decide(messages)
                if not isinstance(decision, Decision):
                    raise TypeError("Model.decide must return a Decision")
                action, args = decision.action, decision.args
            except DecisionError as exc:
                # Invalid arguments or batches never reach repository tools.
                # Feed the rejection back within the same max_steps budget.
                self._record({"type": "decision_error", "step": step, "error": str(exc),
                              "response": exc.message, "recoverable": True})
                messages.extend(exc.feedback_messages())
                continue
            except Exception as exc:
                # Record failure, don't count hallucinated actions as successful tests.
                err = f"Model failure: {type(exc).__name__}: {exc}"
                self._record({"type": "error", "step": step, "error": err})
                return {"status": "error", "reason": err, "steps": step,
                        "last_test_passed": last_test_passed, "trace": str(self.trace_path)}
            self._record({"type": "decision", "step": step, **decision.as_dict(),
                          "tool_call_id": decision.tool_call_id})
            if action == "finish":
                answer = args["answer"]
                # 'finished' is agent reported, not automatic proof of correctness.
                verified_local_change = applied_patches > 0 and last_test_passed is True
                self._record({"type": "finish", "answer": answer, "last_test_passed": last_test_passed,
                              "verified_local_change": verified_local_change})
                return {"status": "agent_reported_finished", "answer": answer,
                        "steps": step, "tool_actions": actions, "last_test_passed": last_test_passed,
                        "verified_local_change": verified_local_change,
                        "trace": str(self.trace_path)}
            observation = self.tools.execute(action, args)
            actions += 1
            if action in ("apply_patch", "replace_text", "replace_lines") and observation["ok"] and observation["data"].get("applied"):
                applied_patches += 1
                # Any edit invalidates a previously passing test result.
                last_test_passed = None
            if action == "run_tests":
                last_test_passed = observation.get("data", {}).get("passed") if observation["ok"] else False
            self._record({"type": "observation", "step": step, "action": action, "result": observation})
            messages.append(decision.assistant_message)
            messages.append(decision.feedback_message(observation))
            # This bounded MVP run retains its entire dialogue. Longer runs will
            # need an explicit token budget and evidence-preserving compaction.
        self._record({"type": "limit", "max_steps": self.max_steps, "last_test_passed": last_test_passed})
        return {"status": "step_limit", "steps": self.max_steps,
                "tool_actions": actions, "last_test_passed": last_test_passed,
                "trace": str(self.trace_path)}
