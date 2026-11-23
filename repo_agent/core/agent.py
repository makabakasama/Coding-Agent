"""Small observe-decide-act loop with durable local JSONL trace."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Protocol
import uuid

from repo_agent.core.prompts import SYSTEM_PROMPT
from repo_agent.core.check_quality import delivery_verdict
from repo_agent.core.context import RunContext
from repo_agent.core.decisions import Decision, DecisionError, TOOL_DEFINITIONS
from repo_agent.core.progress import RunProgress
from repo_agent.core.tools import RepoTools


class Model(Protocol):
    def decide(self, messages: list[dict]) -> Decision: ...


class Agent:
    def __init__(self, model: Model, tools: RepoTools, *, trace_dir: Path,
                 max_steps: int = 50, token_budget=None):
        if not 1 <= max_steps <= 50:
            raise ValueError("max_steps must be in 1..50")
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.token_budget = token_budget
        trace_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = trace_dir / f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}.jsonl"

    def _record(self, item: dict):
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def run_conversation(model, messages: list[dict], *, tools: RepoTools | None,
                         response_limits: dict, on_event=None):
        """Run one conversation turn with the same Agent tool decision protocol.

        The caller supplies the persisted cross-turn context and the tool policy.
        Only observations from this run are appended here; later user messages
        belong to a later queued turn and never enter an active model request.
        """
        from repo_agent.core.provider import TextReply, ToolCallReply

        allowed = {'list_files', 'search_code', 'read_file'} if tools is not None else set()
        definitions = [item for item in TOOL_DEFINITIONS
                       if item['function']['name'] in allowed]
        dialogue = list(messages)
        emit = on_event or (lambda value: None)
        step = 0
        while True:
            step += 1
            emit({'type': 'agent_step', 'decisionIndex': step})
            should_finalize = getattr(model, 'should_finalize', None)
            final_only = bool(allowed and callable(should_finalize)
                              and should_finalize(dialogue, definitions))
            offered = [] if final_only else definitions
            request_messages = (dialogue + [{'role': 'user', 'content':
                'The turn resource budget is nearly exhausted. Answer now from saved observations; '
                'state what remains uncertain. Do not call another tool.'}]
                if final_only else dialogue)
            if final_only:
                emit({'type': 'final_answer_requested', 'decisionIndex': step})
            try:
                reply = model.respond(request_messages, tools=offered,
                                      response_limits=response_limits)
            except DecisionError as exc:
                if final_only:
                    from repo_agent.core.token_budget import TurnBudgetExceeded
                    raise TurnBudgetExceeded('Model did not provide a final answer within the turn budget') from exc
                emit({'type': 'decision_error', 'decisionIndex': step,
                      'message': str(exc)})
                dialogue.extend(exc.feedback_messages(
                    hint=('Call one offered read tool or answer directly in text.' if allowed
                          else 'Answer directly in text; this turn has no tools.')))
                continue
            if isinstance(reply, TextReply):
                emit({'type': 'agent_reply', 'decisionIndex': step})
                return reply, step
            if not isinstance(reply, ToolCallReply):
                raise TypeError('Model.respond must return a text or tool reply')
            if final_only:
                from repo_agent.core.token_budget import TurnBudgetExceeded
                raise TurnBudgetExceeded('Model requested a tool after final-answer-only instruction')
            decision = reply.decision
            if decision.action not in allowed or tools is None:
                raise DecisionError('Tool is unavailable in this turn',
                                    decision.assistant_message)
            emit({'type': 'tool_decision', 'decisionIndex': step,
                  'action': decision.action, 'args': decision.args})
            observation = tools.execute(decision.action, decision.args)
            emit({'type': 'tool_observation', 'decisionIndex': step,
                  'action': decision.action, 'result': observation})
            dialogue.extend((decision.assistant_message,
                             decision.feedback_message(observation)))

    def run(self, task: str) -> dict:
        definitions = getattr(self.model, 'get_tool_definitions', lambda: TOOL_DEFINITIONS)()
        context = RunContext(SYSTEM_PROMPT, task, definitions)
        progress = RunProgress()
        start = {"type": "start", "task": task, "repo": str(self.tools.root),
                 "decision_protocol": "native_tool_calls", "max_steps": self.max_steps,
                 "step_budget_unit": "model_decisions", "prefix_sha256": context.prefix_sha256,
                 "tools_sha256": context.tools_sha256}
        metadata = getattr(self.model, "get_run_metadata", None)
        if callable(metadata):
            start["model_runtime"] = metadata()
        self._record(start)
        actions = 0
        require_check = getattr(self.tools, 'require_check_before_completion', False) is True
        implementation = getattr(self.tools, 'contract', {}).get('kind') in ('fix_bug', 'add_feature')
        prepare_finalization = getattr(self.model, 'prepare_finalization', None)
        fallback_finalization = getattr(self.model, 'fallback_finalization', None)

        def result(status, step, **fields):
            return {"status": status, "steps": step, "tool_actions": actions,
                    "last_test_passed": progress.last_test_passed, "trace": str(self.trace_path),
                    "remaining_decisions": max(0, self.max_steps - step),
                    "progress": progress.snapshot(), **fields,
                    **delivery_verdict(status, fields.get('outcome') if status == 'agent_reported_finished' else None,
                                       progress.developer_verification or {},
                                       assess=implementation or require_check)}

        def final_state(step):
            return {"progress": progress.snapshot(),
                    "budget": {"used_decisions": step, "max_decisions": self.max_steps,
                               "remaining_decisions": max(0, self.max_steps - step)}}

        def interrupted(status, step, reason, **fields):
            if callable(fallback_finalization):
                prepare_finalization(progress, require_check=require_check, implementation=implementation)
                report = fallback_finalization(status, reason)
                self._record({'type': 'evaluation_finalization_fallback', 'step': step, 'report': report})
                fields.update(outcome='incomplete', finalizationReport=report)
            return result(status, step, reason=reason, **fields)

        for step in range(1, self.max_steps + 1):
            runtime_state = progress.runtime_state(step, self.max_steps, require_check=require_check)
            messages, context_metadata = context.assemble(runtime_state)
            self._record({"type": "context", "step": step, "runtime_state": runtime_state,
                          "context": context_metadata, "progress": progress.snapshot()})
            # Optional model events are logged immediately with the current step.
            # Fake/offline models need only implement decide(); no HTTP knowledge here.
            set_sink = getattr(self.model, "set_event_sink", None)
            previous_sink = None
            try:
                if callable(prepare_finalization):
                    prepare_finalization(progress, require_check=require_check,
                                         implementation=implementation)
                elif self.token_budget is not None:
                    used = self.token_budget.require(messages, definitions)
                    self._record({'type': 'token_budget', 'step': step,
                                  'estimatedInputTokens': used,
                                  'maxInputTokens': self.token_budget.input_tokens,
                                  'counter': self.token_budget.counter_name})
                if callable(set_sink):
                    previous_sink = set_sink(lambda event: self._record({**event, "step": step}))
                limited_decide = getattr(self.model, 'decide_with_limits', None)
                decision = (limited_decide(messages, {'max_tokens': self.token_budget.output_tokens,
                                                      'max_input_bytes': 2_000_000})
                            if self.token_budget is not None and callable(limited_decide)
                            else self.model.decide(messages))
                if not isinstance(decision, Decision):
                    raise TypeError("Model.decide must return a Decision")
                action, args = decision.action, decision.args
            except DecisionError as exc:
                # Invalid arguments or batches never reach repository tools.
                # Feed the rejection back within the same max_steps budget.
                self._record({"type": "decision_error", "step": step, "error": str(exc),
                              "response": exc.message, "recoverable": True})
                if getattr(self.model, 'finish_only', False) is True:
                    return interrupted('finalization_failed', step, str(exc), error_code='EVALUATION_FINALIZATION_FAILED')
                context.extend(exc.feedback_messages())
                continue
            except Exception as exc:
                # Record failure, don't count hallucinated actions as successful tests.
                err = f"Model failure: {type(exc).__name__}: {exc}"
                code = (str(exc).split(':', 1)[0]
                        if str(exc).startswith('EVALUATION_') else None)
                status = ('budget_exhausted' if code == 'EVALUATION_TOKEN_BUDGET'
                          else 'finalization_failed' if code == 'EVALUATION_FINALIZATION_FAILED'
                          else 'error')
                self._record({"type": "error", "step": step, "error": err,
                              "error_code": code, "status": status, **final_state(step)})
                return interrupted(status, step, err, error_code=code)
            finally:
                if callable(set_sink):
                    set_sink(previous_sink)
            self._record({"type": "decision", "step": step, **decision.as_dict(),
                          "tool_call_id": decision.tool_call_id})
            if action == "finish":
                answer = args["answer"]
                outcome = args.get('outcome', 'completed')
                evidence = args.get('evidence', '').strip()
                issues = progress.completion_issues(outcome, require_check=require_check)
                missing_edit = implementation and progress.edit_revision == 0 and (outcome == 'completed' or not evidence)
                if missing_edit:
                    issues.insert(0,
                        'No successful code edit has been observed for this implementation task. '
                        'Continue implementing the authorized change. If code already satisfies the request, '
                        'finish with outcome=already_satisfied and evidence naming inspected paths and behavior. '
                        'If blocked or incomplete, use that outcome and state the concrete blocker or remaining work. '
                        'Test status does not replace business-code implementation evidence. '
                        'Do not manufacture an edit to finish.')
                if issues:
                    feedback = {'ok': False, 'error': '\n'.join(issues)}
                    check_missing = (require_check and outcome in ('completed', 'already_satisfied')
                                     and progress.last_test_passed is not True)
                    self._record({'type': 'finish_rejected', 'step': step,
                                  'reason': 'NO_IMPLEMENTATION_EVIDENCE' if missing_edit else
                                            'CHECK_REQUIRED' if check_missing else 'PLAN_INCONSISTENT',
                                  **final_state(step)})
                    if getattr(self.model, 'finish_only', False) is True:
                        return interrupted('finalization_failed', step, feedback['error'],
                                           error_code='EVALUATION_FINALIZATION_FAILED')
                    context.observe(decision, feedback)
                    continue
                # 'finished' is agent reported, not automatic proof of correctness.
                verified_local_change = progress.edit_revision > 0 and progress.last_test_passed is True
                self._record({"type": "finish", "answer": answer, "last_test_passed": progress.last_test_passed,
                              "outcome": outcome, "evidence": evidence,
                              "verified_local_change": verified_local_change, **final_state(step)})
                complete_run = getattr(self.model, 'complete_run', None)
                if callable(complete_run):
                    complete_run()
                return result("agent_reported_finished", step, answer=answer, outcome=outcome,
                              evidence=evidence, verified_local_change=verified_local_change)
            if action == 'update_plan':
                try:
                    progress.update(**args)
                    observation = {'ok': True, 'data': {'progress': progress.snapshot()}}
                except ValueError as exc:
                    observation = {'ok': False, 'error': str(exc)}
            else:
                observation = self.tools.execute(action, args)
                identifier = progress.observe(action, observation, target=args.get('target'))
                observation = {**observation, 'observation_id': identifier}
            actions += 1
            self._record({"type": "observation", "step": step, "action": action,
                          "result": observation, **final_state(step)})
            context.observe(decision, observation)
            if action == 'update_plan' and observation['ok']:
                self._record({'type': 'progress_updated', 'step': step, **final_state(step)})
            # This bounded MVP run retains its entire dialogue. Longer runs will
            # need an explicit token budget and evidence-preserving compaction.
        complete_run = getattr(self.model, 'complete_run', None)
        if callable(complete_run):
            complete_run()
        self._record({"type": "limit", "max_steps": self.max_steps,
                      "last_test_passed": progress.last_test_passed, **final_state(self.max_steps)})
        if callable(fallback_finalization):
            return interrupted('step_limit', self.max_steps, 'Decision limit reached before an accepted finish')
        return result("step_limit", self.max_steps)
