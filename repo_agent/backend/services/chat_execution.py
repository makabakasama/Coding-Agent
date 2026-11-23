"""Worker-side text discussion, durable evidence and interrupted-result recovery."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import time

from repo_agent.backend.schemas.chat_contract import validate_chat_id
from repo_agent.backend.schemas.task_contract import TaskError, load_json
from repo_agent.backend.services.task_execution_tools import TaskRepoTools
from repo_agent.core.agent import Agent
from repo_agent.core.chat_context import build_chat_context
from repo_agent.core.decisions import DecisionError, TOOL_DEFINITIONS
from repo_agent.core.provider import ContextLimitError, OpenAICompatibleProvider, ProviderError, TextReply
from repo_agent.core.token_budget import TokenBudget, TurnBudgetExceeded
from repo_agent.infrastructure.file_io import write_json, now
from repo_agent.infrastructure.redaction import Redactor


logger = logging.getLogger('repo_agent.chat')
READ_TOOLS = [item for item in TOOL_DEFINITIONS
              if item['function']['name'] in {'list_files', 'search_code', 'read_file'}]


class _BudgetedModel:
    """Check every model request, including requests after tool observations."""
    def __init__(self, model, budget, record, *, total_tokens, max_seconds):
        self.model, self.budget, self.record = model, budget, record
        self.total_tokens, self.max_seconds = total_tokens, max_seconds
        self.charged_tokens = 0
        self.started = time.monotonic()

    def should_finalize(self, messages, tools):
        if not tools:
            return False
        prompt = self.budget.count(messages, tools)
        remaining = self.total_tokens - self.charged_tokens
        return (remaining <= 2 * (prompt + self.budget.output_tokens) + 4096
                or self.max_seconds - (time.monotonic() - self.started)
                <= min(120, self.max_seconds * 0.2))

    def respond(self, messages, *, tools, response_limits):
        if time.monotonic() - self.started >= self.max_seconds:
            raise TurnBudgetExceeded('Chat turn time allowance exhausted')
        used = self.budget.require(messages, tools)
        charge = used + self.budget.output_tokens
        if self.charged_tokens + charge > self.total_tokens:
            raise TurnBudgetExceeded('Chat turn cumulative token allowance exhausted')
        self.charged_tokens += charge
        self.record(used, self.charged_tokens)
        respond = getattr(self.model, 'respond_stream', self.model.respond)
        return respond(messages, tools=tools, response_limits=response_limits)


class ChatExecution:
    def __init__(self, store, *, projects=None, task_service=None, model_factory=None,
                 token_counter_factory=None, max_input_bytes=2_000_000,
                 max_output_tokens=2048):
        self.store, self.model_factory = store, model_factory
        self.token_counter_factory = token_counter_factory or TokenBudget
        self.projects = projects
        self.task_service = task_service
        self.runs_root = store.store.runs_root
        self.configure(max_input_bytes=max_input_bytes, max_output_tokens=max_output_tokens)

    def configure(self, *, max_input_bytes, max_output_tokens, context_tokens=128_000,
                  reserve_tokens=4_096, tokenizer='auto', max_turn_tokens=250_000,
                  max_turn_seconds=600):
        if (any(type(value) is not int or value < 1 for value in
                (max_input_bytes, max_output_tokens, context_tokens, reserve_tokens,
                 max_turn_tokens, max_turn_seconds))
                or context_tokens <= max_output_tokens + reserve_tokens
                or max_turn_tokens <= max_output_tokens
                or not isinstance(tokenizer, str) or not tokenizer):
            raise ValueError('Chat request limits must be positive integers')
        self.limits = {'max_input_bytes': max_input_bytes, 'max_tokens': max_output_tokens}
        self.token_limits = {'context_tokens': context_tokens,
                             'output_tokens': max_output_tokens,
                             'reserve_tokens': reserve_tokens, 'tokenizer': tokenizer}
        self.turn_limits = {'maxEstimatedTokens': max_turn_tokens,
                            'maxSeconds': max_turn_seconds}
        if self.task_service is not None:
            execution = self.task_service.execution
            getattr(execution, 'execution', execution).chat_token_limits = dict(self.token_limits)

    def _directory(self, claim, *, create=False):
        for field in ('conversationId', 'turnId', 'claimId'):
            validate_chat_id(claim[field])
        path = self.runs_root / 'chat' / claim['conversationId'] / claim['turnId'] / claim['claimId']
        if path.resolve() != path:
            raise TaskError(503, 'CHAT_EVIDENCE_UNAVAILABLE', '会话材料路径不符合要求')
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _save(self, directory, name, value, redactor):
        path = directory / name
        if path.resolve() != path:
            raise TaskError(503, 'CHAT_EVIDENCE_UNAVAILABLE', '会话附件路径不符合要求')
        clean = redactor.private_value(value)
        write_json(path, clean)
        return {'path': path.relative_to(self.runs_root).as_posix(),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'redacted': clean != value}

    def _history_observations(self, turn, history):
        """Select durable read evidence, without replaying old model or HTTP logs."""
        observations = []
        seen = set()
        for entry in history:
            message = entry['message']
            prior_id = message['turnId']
            if prior_id == turn['turnId'] or prior_id in seen:
                continue
            seen.add(prior_id)
            reference = (entry.get('artifacts') or {}).get('trace')
            if reference is None:
                continue
            relative = reference.get('path') if isinstance(reference, dict) else None
            expected = reference.get('sha256') if isinstance(reference, dict) else None
            parts = Path(relative).parts if isinstance(relative, str) else ()
            if (len(parts) != 5 or parts[:3] != ('chat', turn['conversationId'], prior_id)
                    or parts[4] != 'trace.json' or not isinstance(expected, str)
                    or len(expected) != 64):
                raise TaskError(409, 'CHAT_EVIDENCE_INVALID', '历史观察材料引用无效')
            path = self.runs_root.joinpath(*parts)
            if path.resolve() != path:
                raise TaskError(409, 'CHAT_EVIDENCE_INVALID', '历史观察材料路径无效')
            try:
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError('Trace hash mismatch')
                trace = load_json(raw)
                if not isinstance(trace, list):
                    raise ValueError('Trace is not a list')
            except (OSError, ValueError, UnicodeError, TypeError) as exc:
                raise TaskError(503, 'CHAT_EVIDENCE_UNAVAILABLE', '历史观察材料无法核实') from exc
            calls = {}
            for item in trace:
                if not isinstance(item, dict):
                    raise TaskError(409, 'CHAT_EVIDENCE_INVALID', '历史观察格式无效')
                index = item.get('decisionIndex')
                if type(index) is not int or index < 1:
                    continue
                if item.get('type') == 'tool_decision':
                    calls[index] = item
                elif item.get('type') == 'tool_observation':
                    call = calls.get(index)
                    action, result = item.get('action'), item.get('result')
                    if (call is None or call.get('action') != action
                            or not isinstance(call.get('args'), dict)
                            or action not in ('read_file', 'search_code', 'list_files')
                            or not isinstance(result, dict) or type(result.get('ok')) is not bool):
                        raise TaskError(409, 'CHAT_EVIDENCE_INVALID', '历史工具调用与结果不匹配')
                    data = result.get('data') if isinstance(result.get('data'), dict) else {}
                    observations.append({'observationId': f'chat_obs_{prior_id}_{index}',
                                         'turnId': prior_id, 'action': action,
                                         'args': call.get('args'), 'ok': result.get('ok'),
                                         'file': data.get('file'), 'sha256': data.get('sha256'),
                                         'start': data.get('start'), 'end': data.get('end'),
                                         'result': result, 'recordedAt': item.get('at'),
                                         'traceSha256': expected,
                                         'allowedTools': (entry.get('policySnapshot') or {}).get('allowedTools', [])})
        question = history[-1]['message']['content'].casefold()
        selected = set(range(max(0, len(observations) - 3), len(observations)))
        if any(word in question for word in ('读过', '读了', '读取', '看过', '做了什么',
                                             '工具', '历史', 'read', 'previously inspected')):
            selected.update(index for index, item in enumerate(observations)
                            if item['action'] == 'read_file')
        for index, item in enumerate(observations):
            path = item.get('file') or (item.get('args') or {}).get('path')
            if isinstance(path, str) and path != '.' and path.casefold() in question:
                selected.add(index)
        by_turn = {}
        for index, item in enumerate(observations):
            record = {key: value for key, value in item.items() if key != 'result'}
            if index in selected:
                record['result'] = item['result']
            else:
                record['resultOmitted'] = '历史结果未被本轮选中；可按观察引用核对或重新读取当前文件'
            by_turn.setdefault(item['turnId'], []).append(record)
        for entry in history:
            if entry['message']['role'] == 'assistant' or entry['turnStatus'] in ('failed', 'unknown'):
                entry['programObservations'] = by_turn.pop(entry['message']['turnId'], [])
        return {'totalObservations': len(observations),
                'fullResultsSelected': len(selected),
                'metadataOnly': len(observations) - len(selected)}

    def _bounded_context(self, turn, history, definitions, budget):
        current_id = turn['turnId']
        prior_ids = list(dict.fromkeys(entry['message']['turnId'] for entry in history
                                       if entry['message']['turnId'] != current_id))
        protected = {current_id, *prior_ids[-1:]}
        question = history[-1]['message']['content'].casefold()
        for entry in history:
            if any('result' in record for record in entry.get('programObservations', [])):
                protected.add(entry['message']['turnId'])
            if (entry.get('executionEvidence') is not None
                    and any(word in question for word in ('修改', '改动', '结果', '检查', '测试', 'changed', 'test'))):
                protected.add(entry['message']['turnId'])
        initial_messages, _ = build_chat_context(turn, history, tool_definitions=definitions)
        initial_tokens = budget.count(initial_messages, definitions)
        omitted = []
        remaining = history
        while True:
            messages, manifest = build_chat_context(turn, remaining,
                                                    tool_definitions=definitions,
                                                    omitted_turn_count=len(omitted))
            used = budget.count(messages, definitions)
            if used <= budget.input_tokens:
                break
            candidate = next((identifier for identifier in prior_ids
                              if identifier not in protected and identifier not in omitted), None)
            if candidate is None:
                raise ContextLimitError('必要的当前需求与历史观察超过模型 token 预算；请缩小问题或提高已核实的模型窗口')
            omitted.append(candidate)
            remaining = [entry for entry in history if entry['message']['turnId'] not in omitted]
        manifest['selection'] = {
            'initialEstimatedTokens': initial_tokens, 'finalEstimatedTokens': used,
            'omittedTurnIds': omitted,
            'omittedMessageIds': [entry['message']['messageId'] for entry in history
                                  if entry['message']['turnId'] in omitted],
            'omittedObservationIds': [record['observationId'] for entry in history
                                       if entry['message']['turnId'] in omitted
                                       for record in entry.get('programObservations', [])],
        }
        return messages, manifest, used

    def _read_tools(self, workspace_ref):
        if workspace_ref is None:
            return None
        project = self.projects._projects().get(workspace_ref) if self.projects is not None else None
        if project is None:
            raise TaskError(400, 'UNKNOWN_PROJECT', '会话关联项目已不可用')
        path = Path(project['repo'])
        if not path.is_absolute():
            path = self.projects.config_path.parent / path
        try:
            root = path.resolve(strict=True)
            if not root.is_dir():
                raise OSError('Project root is not a directory')
            contract = {'kind': 'read_code', 'allowTests': False, 'allowedPaths': [],
                        'checkTargets': [], 'workspaceMode': 'direct',
                        'sourceKind': project.get('sourceKind', 'git')}
            return TaskRepoTools(root, contract=contract,
                                 excluded_roots=(self.runs_root,))
        except (OSError, ValueError) as exc:
            raise TaskError(503, 'PROJECT_UNAVAILABLE', '会话关联项目暂不可读取') from exc

    def run_claimed(self, claim):
        if self.store.claimed_turn(claim)['mode'] == 'execute':
            self._bridge_claimed(claim)
            return
        started = time.monotonic()
        redactor, directory = None, None
        artifacts, trace = {}, []
        metrics = {'modelDecisions': 0, 'httpAttempts': 0, 'usageReports': 0,
                   'completeUsageReports': 0,
                   'toolActions': 0, 'usage': {}, 'usageComplete': False}
        decision_index = 1

        def event(value):
            nonlocal decision_index
            kind = value['type']
            if kind == 'model_text':
                # Redact the accumulated text, including secrets split across chunks.
                text = value['text']
                if not value.get('complete'):
                    hold = max((size for secret in redactor.secrets
                                for size in range(1, min(len(secret), len(text)) + 1)
                                if text.endswith(secret[:size])), default=0)
                    if hold:
                        text = text[:-hold]
                self.store.record_event(claim, {
                    'type': kind, 'text': redactor.private(text),
                    'decisionIndex': decision_index, 'callId': claim['claimId'],
                })
                return
            if kind == 'agent_step':
                decision_index = value['decisionIndex']
                metrics['modelDecisions'] = decision_index
            elif kind == 'tool_observation':
                metrics['toolActions'] += 1
            if kind == 'model_input':
                key = 'request' if decision_index == 1 else f'request{decision_index}'
                name = 'request.json' if decision_index == 1 else f'request-{decision_index}.json'
                artifacts[key] = self._save(directory, name, value, redactor)
                record = {'type': kind, 'requestBytes': value['requestBytes'],
                          'requestSha256': value['requestSha256'], 'artifact': artifacts[key]}
            elif kind == 'model_response':
                key = 'response' if decision_index == 1 else f'response{decision_index}'
                name = 'response.json' if decision_index == 1 else f'response-{decision_index}.json'
                artifacts[key] = self._save(directory, name, value, redactor)
                record = {'type': kind, 'artifact': artifacts[key]}
            else:
                record = value
            record = redactor.private_value({**record, 'callId': claim['claimId'],
                                             'decisionIndex': decision_index})
            trace.append({**record, 'at': now()})
            artifacts['trace'] = self._save(directory, 'trace.json', trace, redactor)
            if kind == 'model_request_attempt' and value.get('outcome') == 'started':
                metrics['httpAttempts'] += 1
            if kind == 'model_usage':
                metrics['usageReports'] += 1
                raw = value['usage']
                if (any(key in raw for key in ('prompt_tokens', 'input_tokens'))
                        and any(key in raw for key in ('completion_tokens', 'output_tokens'))):
                    metrics['completeUsageReports'] += 1
                # Provider aliases describe the same tokens and must not be added twice.
                for canonical, names in (('inputTokens', ('prompt_tokens', 'input_tokens')),
                                         ('outputTokens', ('completion_tokens', 'output_tokens')),
                                         ('cachedTokens', ('cached_tokens',))):
                    for name in names:
                        if name in raw:
                            metrics['usage'][canonical] = metrics['usage'].get(canonical, 0) + raw[name]
                            break
            self.store.record_event(claim, record)

        model, previous_sink = None, None
        sink_attached = False
        try:
            redactor = Redactor()
            directory = self._directory(claim, create=True)
            turn, history = self.store.context(claim, max_input_bytes=self.limits['max_input_bytes'])
            policy = turn['policySnapshot']
            if turn['mode'] != 'discuss' or policy['allowWrites'] or policy['allowTests']:
                raise TaskError(409, 'CHAT_POLICY_UNSUPPORTED', '讨论轮不允许修改或运行检查')
            expected_tools = {'list_files', 'search_code', 'read_file'} if policy['workspaceRef'] else set()
            selected_tools = set(policy['allowedTools'])
            if selected_tools not in (set(), expected_tools):
                raise TaskError(409, 'CHAT_POLICY_UNSUPPORTED', '本轮工具权限快照无效')
            tools = self._read_tools(policy['workspaceRef']) if selected_tools else None
            definitions = READ_TOOLS if tools is not None else []
            observation_selection = self._history_observations(turn, history)
            model = self.model_factory() if self.model_factory is not None else OpenAICompatibleProvider()
            source = 'injected_test_model' if self.model_factory is not None else 'configured_provider'
            budget = self.token_counter_factory(getattr(model, 'model', 'injected-model'),
                                                **self.token_limits)
            messages, manifest, initial_tokens = self._bounded_context(turn, history, definitions, budget)
            manifest.update(source=source, responseLimits=self.limits,
                            observationSelection=observation_selection,
                            tokenBudget=budget.manifest(initial_tokens),
                            turnBudget=self.turn_limits,
                            model=getattr(model, 'model', None), profile=getattr(model, 'profile', None),
                            runtime=getattr(model, 'get_run_metadata', lambda: {})(),
                            messages=messages, redaction='credential-values-v1')
            artifacts['context'] = self._save(directory, 'context.json', manifest, redactor)
            event({'type': 'context_ready', 'artifact': artifacts['context']})
            if len(json.dumps(messages).encode('utf-8')) > self.limits['max_input_bytes']:
                raise ContextLimitError('Conversation messages exceed the input limit')
            set_sink = getattr(model, 'set_event_sink', None)
            if callable(set_sink):
                previous_sink = set_sink(event)
                sink_attached = True
            guarded = _BudgetedModel(model, budget, lambda used, total: event({
                'type': 'token_budget', 'estimatedInputTokens': used,
                'maxInputTokens': budget.input_tokens, 'counter': budget.counter_name,
                'cumulativeReservedTokens': total,
                'maxTurnTokens': self.turn_limits['maxEstimatedTokens']}),
                total_tokens=self.turn_limits['maxEstimatedTokens'],
                max_seconds=self.turn_limits['maxSeconds'])
            reply, _ = Agent.run_conversation(guarded, messages, tools=tools,
                                               response_limits=dict(self.limits),
                                               on_event=event)
            if (not isinstance(reply, TextReply) or not isinstance(reply.text, str)
                    or not reply.text.strip() or '\x00' in reply.text):
                raise TaskError(400, 'MODEL_PROTOCOL_ERROR', '纯文本讨论未返回有效文本')
            reply.text.encode('utf-8')
            artifacts['reply'] = self._save(directory, 'reply.json', {
                'assistantMessage': reply.assistant_message, 'text': reply.text,
            }, redactor)
            metrics['queueSeconds'] = (datetime.fromisoformat(turn['startedAt'].replace('Z', '+00:00'))
                                       - datetime.fromisoformat(turn['createdAt'].replace('Z', '+00:00'))).total_seconds()
            result = {'status': 'completed', 'stopReason': 'RESPONSE_SAVED',
                      'text': redactor.private(reply.text)}
        except Exception as exc:
            if isinstance(exc, ContextLimitError):
                reason = 'CONTEXT_LIMIT'
            elif isinstance(exc, TurnBudgetExceeded):
                reason = 'TURN_BUDGET'
            elif isinstance(exc, TaskError):
                reason = exc.code
            elif isinstance(exc, DecisionError):
                reason = 'MODEL_PROTOCOL_ERROR'
            elif isinstance(exc, ProviderError):
                reason = 'PROVIDER_ERROR'
            elif isinstance(exc, OSError):
                reason = 'CHAT_EVIDENCE_UNAVAILABLE'
            else:
                reason = 'CHAT_EXECUTION_ERROR'
            logger.warning('turn=%s failed reason=%s error_type=%s', claim['turnId'], reason, type(exc).__name__)
            result = {'status': 'failed', 'stopReason': reason, 'errorType': type(exc).__name__}
            message = '讨论执行失败，请核对本轮材料'
            if redactor is not None and isinstance(exc, (TaskError, ProviderError, DecisionError)):
                message = redactor.private(str(exc))[:400]
            result['error'] = {'type': type(exc).__name__, 'message': message}
        finally:
            if sink_attached:
                model.set_event_sink(previous_sink)
        metrics['processingSeconds'] = time.monotonic() - started
        metrics['usageComplete'] = (metrics['httpAttempts'] == metrics['usageReports']
                                    == metrics['completeUsageReports'] > 0
                                    and {'inputTokens', 'outputTokens'} <= metrics['usage'].keys())
        result.update(schemaVersion=1, claim=claim, recordedAt=now(),
                      metrics=metrics, artifacts=dict(artifacts))
        # The durable outcome precedes DB completion, allowing recovery without another model call.
        if directory is not None and redactor is not None:
            try:
                artifacts['result'] = self._save(directory, 'result.json', result, redactor)
            except (OSError, ValueError, TaskError):
                result.update(status='failed', stopReason='CHAT_EVIDENCE_UNAVAILABLE')
                result.pop('text', None)
                logger.warning('turn=%s result_evidence_not_saved', claim['turnId'])
        result['artifacts'] = artifacts
        try:
            self.store.finish(claim, result)
        except TaskError as exc:
            if exc.code != 'CHAT_CLAIM_LOST':
                raise
            logger.warning('turn=%s late_result_preserved claim_lost', claim['turnId'])

    def _bridge_claimed(self, claim):
        """Create and queue one existing coding task without making a Chat model call."""
        task_id, created, linked, enqueued = claim['turnId'], False, False, False
        try:
            turn, history = self.store.context(claim, max_input_bytes=self.limits['max_input_bytes'])
            if self.task_service is None or turn['mode'] != 'execute':
                raise TaskError(409, 'CHAT_POLICY_UNSUPPORTED', '执行处理器不可用')
            contract = dict(turn['policySnapshot']['executionContract'])
            frozen_commit = turn['policySnapshot'].get('frozenBaseCommit')
            if frozen_commit is not None:
                contract['revision'] = frozen_commit
            self._history_observations(turn, history)
            coding = getattr(self.task_service.execution, 'execution', self.task_service.execution)
            coding_model = coding.model_factory()
            limits = {**self.token_limits,
                      'reserve_tokens': self.token_limits['reserve_tokens'] + 8192}
            budget = self.token_counter_factory(getattr(coding_model, 'model', 'injected-model'),
                                                **limits)
            messages, manifest, _ = self._bounded_context(turn, history, TOOL_DEFINITIONS, budget)
            contract['current'] = history[-1]['message']['content']
            contract['conversationContext'] = json.dumps({
                'schemaVersion': 1, 'messages': messages[1:-1],
                'selection': manifest['selection'],
                'observationIds': [origin['observationIds'] for origin in manifest['origins']
                                   if origin['kind'] == 'program_observation'],
            }, ensure_ascii=False, sort_keys=True)
            if len(contract['conversationContext']) > 250_000:
                raise TaskError(400, 'CONTEXT_LIMIT', '必要的会话上下文超过执行材料存储上限')
            task = self.task_service.create(contract, task_id=task_id)
            created = True
            self.store.linked_task(claim, task['taskId'])
            linked = True
            prepared = self.task_service.prepare(task_id, {})
            if prepared['status'] != 'prepared':
                raise TaskError(409, 'TASK_PREPARATION_FAILED', '任务工作副本未准备完成')
            _, started = self.task_service.execution.start(task_id, {})
            if not started.get('executionId'):
                raise TaskError(503, 'EXECUTION_UNCONFIRMED', '任务入队结果缺少执行编号')
            enqueued = True
            self.store.bridge_started(claim, task_id, started['executionId'])
        except Exception as exc:
            # A committed task dispatch must never be described as a failed
            # Chat turn. Recovery will inspect the same preallocated task id.
            if enqueued or created and not linked or isinstance(exc, TaskError) and exc.code == 'STORAGE_UNAVAILABLE':
                raise
            try:
                state = self.task_service.execution.get(task_id, check_readiness=False, fresh=True)
            except TaskError as lookup:
                if lookup.code != 'TASK_NOT_FOUND':
                    raise
            else:
                if state['status'] != 'not_started':
                    raise
            reason = exc.code if isinstance(exc, TaskError) else 'CHAT_BRIDGE_ERROR'
            self.store.finish(claim, {'status': 'failed', 'stopReason': reason,
                                      'error': {'type': type(exc).__name__,
                                                'message': str(exc)[:400] if isinstance(exc, TaskError)
                                                else '执行准备失败，请核对任务记录'}})

    def reconcile(self):
        """Project finished coding tasks back into Chat without occupying a slot."""
        if self.task_service is None:
            return
        for claim in self.store.executing():
            try:
                state = self.task_service.execution.get(claim['taskId'], check_readiness=False, fresh=True)
            except TaskError as exc:
                if exc.code in ('TASK_NOT_FOUND', 'TASK_PURGED'):
                    self.store.execution_unknown(claim, 'TASK_UNAVAILABLE')
                    continue
                raise
            if state['status'] == 'unknown':
                self.store.execution_unknown(claim, 'TASK_EXECUTION_UNKNOWN')
                continue
            if state['status'] == 'terminated' and not state.get('resultAvailable'):
                self.store.execution_unknown(claim, 'TASK_RESULT_UNAVAILABLE')
                continue
            if state['status'] != 'terminated':
                continue
            try:
                task_result = self.task_service.execution.result(claim['taskId'])
            except (TaskError, OSError, ValueError, KeyError, TypeError):
                self.store.execution_unknown(claim, 'TASK_RESULT_UNAVAILABLE')
                continue
            completion = task_result.get('completion') or {'status': 'unknown'}
            checks = task_result.get('finalChecks') or {'status': 'unknown'}
            summary = (task_result.get('agent') or {}).get('summary') or 'Agent 未保存文字说明。'
            if len(summary) > 6000:
                summary = summary[:6000] + '\n[说明过长，完整内容请查看编码任务详情]'
            check_status = checks.get('status', 'unknown')
            stop_reason = task_result.get('stopReason', 'UNKNOWN')
            execution = task_result.get('execution_status', completion['status'])
            acceptance = task_result.get('acceptance_status', 'unknown')
            gap = task_result.get('priority_gap')
            text = (f"{summary}\n\n执行状态：{execution}。模型报告：{completion['status']}。"
                    f"验收状态：{acceptance}。停止原因：{stop_reason}。"
                    f"实际检查：{check_status}。任务编号：{claim['taskId']}。")
            if gap:
                text += f"\n待验证：{gap['scenario']}。验证目标：{gap['verification_goal']}"
            evidence = {'taskId': claim['taskId'], 'executionId': claim['executionId'],
                        'completion': completion, 'stopReason': stop_reason,
                        'execution_status': execution, 'acceptance_status': acceptance,
                        'remaining_gaps': task_result.get('remaining_gaps', []),
                        'finalChecks': checks,
                        'changedPaths': (task_result.get('review') or {}).get('changedPaths'),
                        'independentlyVerified': task_result.get('independentlyVerified')}
            self.store.finish(claim, {'status': 'completed', 'stopReason': 'EXECUTION_RECORDED',
                                      'text': text, 'executionEvidence': evidence})

    def recover(self):
        """The caller holds both Worker locks, excluding the previous local process."""
        for claim in self.store.interrupted():
            if self.store.claimed_turn(claim)['mode'] == 'execute':
                self._bridge_claimed(claim)
                continue
            result = {'status': 'failed', 'stopReason': 'WORKER_INTERRUPTED', 'artifacts': {}}
            try:
                directory = self._directory(claim)
                path = directory / 'result.json'
                if path.resolve() != path:
                    raise ValueError('Linked result file')
                if path.exists():
                    saved = load_json(path.read_bytes())
                    if not isinstance(saved, dict) or saved.get('schemaVersion') != 1 or saved.get('claim') != claim:
                        raise ValueError('Result identity mismatch')
                    if saved.get('status') not in ('completed', 'failed') or not isinstance(saved.get('stopReason'), str):
                        raise ValueError('Invalid outcome')
                    if saved['status'] == 'completed' and (not isinstance(saved.get('text'), str)
                                                          or not saved['text'].strip() or '\x00' in saved['text']):
                        raise ValueError('Missing reply')
                    if saved['status'] == 'completed':
                        saved['text'].encode('utf-8')
                    if not isinstance(saved.get('artifacts'), dict):
                        raise ValueError('Invalid artifact manifest')
                    for reference in saved['artifacts'].values():
                        if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
                            raise ValueError('Invalid artifact reference')
                        artifact = self.runs_root / reference['path']
                        if (artifact.parent != directory or artifact.resolve() != artifact
                                or hashlib.sha256(artifact.read_bytes()).hexdigest() != reference['sha256']):
                            raise ValueError('Artifact verification failed')
                    if saved['status'] == 'completed':
                        if not {'context', 'reply'} <= saved['artifacts'].keys():
                            raise ValueError('Incomplete reply evidence')
                        reply_path = self.runs_root / saved['artifacts']['reply']['path']
                        reply = load_json(reply_path.read_bytes())
                        if not isinstance(reply, dict) or reply.get('text') != saved['text']:
                            raise ValueError('Saved reply does not match the outcome')
                    saved['artifacts']['result'] = {
                        'path': path.relative_to(self.runs_root).as_posix(),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    result = saved
            except (OSError, ValueError, KeyError, TypeError, RecursionError, TaskError):
                result['stopReason'] = 'RECOVERY_EVIDENCE_INVALID'
            self.store.finish(claim, result, recovered=True)
            logger.info('turn=%s recovered status=%s reason=%s',
                        claim['turnId'], result['status'], result['stopReason'])
