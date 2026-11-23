"""Offline adapter regressions. Git fixtures only; no models/Docker/evaluator."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from repo_agent.evaluation.artifacts import load_run, save
from repo_agent.evaluation.grader import grading_command, execute_grader
from repo_agent.core.agent import Agent
from repo_agent.core.decisions import Decision, TOOL_DEFINITIONS
from repo_agent.evaluation.runner import run_agent, _BudgetedModel
from repo_agent.core.provider import ProviderError
from repo_agent.evaluation.swebench import export_patch, import_grade, prepare, select_instance


class SWEAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init')
        self.git('config', 'user.name', 'Offline Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.source / 'app.py').write_text('value = 1\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.row = {'instance_id': 'acme__demo-1', 'repo': 'acme/demo', 'base_commit': self.commit,
                    'problem_statement': 'Update the value.', 'version': '1', 'patch': 'HIDDEN_GOLD',
                    'test_patch': 'HIDDEN_TEST_PATCH', 'FAIL_TO_PASS': '["test_target"]',
                    'PASS_TO_PASS': '["test_control"]'}
        self.input = self.root / 'input.json'
        self.input.write_text(json.dumps([self.row]), encoding='utf-8')

    def git(self, *args):
        return subprocess.run(['git', '-c', 'core.hooksPath=' + str(self.root / 'no-hooks'),
                               '-c', 'commit.gpgsign=false',
                               '-c', 'core.autocrlf=false', '-C', str(self.source), *args],
                              check=True, capture_output=True, text=True, timeout=30).stdout

    def prepared(self):
        return prepare(self.input, self.row['instance_id'], self.source,
                       runs_root=self.root / 'runs', workspaces_root=self.root / 'workspaces')

    def export(self, directory):
        _, run, _ = load_run(directory)
        workspace = Path(run['workspace'])
        (workspace / 'app.py').write_text('value = 2\n', encoding='utf-8')
        (workspace / 'new.py').write_text('added = True\n', encoding='utf-8')
        ref = export_patch(directory, workspace, self.commit)
        save(directory, 'attempt.json', {'patch': ref})
        prediction = {'instance_id': self.row['instance_id'], 'model_name_or_path': 'coding-agent',
                      'model_patch': (directory / 'candidate.patch').read_bytes().decode('utf-8')}
        (directory / 'predictions.jsonl').write_text(json.dumps(prediction) + '\n', encoding='utf-8')
        return prediction

    def report(self, *, resolved=True, infra=False):
        path = self.root / 'report.json'
        path.write_text(json.dumps({self.row['instance_id']: {'resolved': resolved,
                        'patch_exists': True, 'patch_successfully_applied': True,
                        'infra_failure': infra}}), encoding='utf-8')
        return path

    def test_prepare_keeps_gold_out_of_task_and_worktree_and_preserves_source(self):
        directory = self.prepared()
        _, run, task = load_run(directory)
        self.assertNotIn('HIDDEN_GOLD', json.dumps(task))
        self.assertFalse((Path(run['workspace']) / 'private-input').exists())
        self.assertEqual('value = 1\n', (self.source / 'app.py').read_text())
        self.assertNotEqual(directory, self.prepared())

    def test_duplicate_task_is_rejected(self):
        self.input.write_text(json.dumps([self.row, self.row]), encoding='utf-8')
        with self.assertRaises(ValueError):
            select_instance(self.input, self.row['instance_id'])

    def test_patch_includes_new_files_and_model_finish_is_not_a_grade(self):
        directory = self.prepared()
        prediction = self.export(directory)
        self.assertIn('new.py', prediction['model_patch'])
        self.assertIn('+value = 2', prediction['model_patch'])
        self.assertFalse((directory / 'grades').exists())
        command = grading_command(directory)
        self.assertEqual('official_runner.py', Path(command[1]).name)
        self.assertEqual(self.commit, command[command.index('--expected-base') + 1])
        self.assertEqual('1', command[command.index('--max_workers') + 1])
        self.assertEqual('value = 1\n', (self.source / 'app.py').read_text())

    def test_grade_rejects_different_patch_and_string_boolean(self):
        directory = self.prepared()
        self.export(directory)
        wrong = self.root / 'wrong.patch'
        wrong.write_bytes(b'wrong patch')
        with self.assertRaises(ValueError):
            import_grade(directory, self.report(), wrong)
        report = self.report()
        value = json.loads(report.read_bytes())
        value[self.row['instance_id']]['resolved'] = 'false'
        report.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(ValueError):
            import_grade(directory, report, directory / 'candidate.patch')

    def test_regrades_append_and_infrastructure_failure_is_unknown(self):
        directory = self.prepared()
        self.export(directory)
        first = import_grade(directory, self.report(), directory / 'candidate.patch')
        second = import_grade(directory, self.report(resolved=False, infra=True), directory / 'candidate.patch')
        self.assertNotEqual(first, second)
        self.assertEqual('pass', json.loads((first / 'grade.json').read_bytes())['status'])
        self.assertEqual('unknown', json.loads((second / 'grade.json').read_bytes())['status'])
        self.assertFalse(json.loads((first / 'grade.json').read_bytes())['independentlyVerified'])

    def test_prediction_tampering_prevents_grading_command(self):
        directory = self.prepared()
        self.export(directory)
        (directory / 'predictions.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            grading_command(directory)

    def test_missing_target_failure_blocks_model_before_attempt_is_started(self):
        directory = self.prepared()
        with patch('repo_agent.evaluation.runner.OpenAICompatibleProvider') as model:
            with self.assertRaises(ValueError):
                run_agent(directory, baseline_report=self.report(resolved=False),
                          max_steps=2, max_total_tokens=10000, max_seconds=10)
        model.assert_not_called()
        self.assertFalse((directory / 'attempt.lock').exists())

    def test_protected_test_edit_cannot_be_exported(self):
        directory = self.prepared()
        _, run, _ = load_run(directory)
        (Path(run['workspace']) / 'test_bypass.py').write_text('assert True\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            export_patch(directory, Path(run['workspace']), self.commit)
        self.assertFalse((directory / 'candidate.patch').exists())

    def test_cumulative_budget_blocks_call_before_spending(self):
        class Counter:
            output_tokens = 10
            def require(self, messages, tools):
                return 100
        class Model:
            calls = 0
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return []
            def decide_with_limits(self, messages, limits):
                self.calls += 1
        raw = Model()
        guarded = _BudgetedModel(raw, Counter(), 100, 30)
        with self.assertRaises(ProviderError):
            guarded.decide([])
        self.assertEqual(0, raw.calls)

    def test_provider_usage_settles_estimate_and_final_reserve_is_released_on_finish(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10
        class Model:
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return [tool for tool in TOOL_DEFINITIONS if tool['function']['name'] == 'finish']
            def get_run_metadata(self):
                return {'transport_retry': {'max_attempts': 1}}
            def decide_with_limits(self, messages, limits):
                self.sink({'type': 'model_usage', 'available': True,
                           'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}})
                return Decision('finish', {'answer': 'Done'})
        guarded = _BudgetedModel(Model(), Counter(), 300, 30)
        guarded.decide([])
        self.assertEqual(8, guarded.accounted_tokens)
        self.assertEqual(1, guarded.settled_calls)
        self.assertEqual(0, guarded.estimated_calls)
        self.assertGreater(guarded.finalization_reserve, 0)
        guarded.complete_run()
        self.assertEqual(0, guarded.accounting()['finalAnswerReserveTokens'])

    def test_missing_usage_and_failed_attempts_are_charged_conservatively(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10
        class Model:
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return [tool for tool in TOOL_DEFINITIONS if tool['function']['name'] == 'finish']
            def get_run_metadata(self):
                return {'transport_retry': {'max_attempts': 2}}
            def decide_with_limits(self, messages, limits):
                self.sink({'type': 'model_request_attempt', 'outcome': 'failed'})
                self.sink({'type': 'model_usage', 'available': True,
                           'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}})
                return Decision('read_file', {'path': 'app.py'})
        guarded = _BudgetedModel(Model(), Counter(), 500, 30)
        guarded.decide([])
        self.assertEqual(40, guarded.accounted_tokens)
        self.assertEqual(0, guarded.settled_calls)
        self.assertEqual(1, guarded.estimated_calls)

    def test_budget_near_limit_forces_finish_only_request(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10 if [tool['function']['name'] for tool in tools] == ['finish'] else 12
        class Model:
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def get_run_metadata(self):
                return {'transport_retry': {'max_attempts': 1}}
            def decide_with_tools(self, messages, limits, tool_names):
                self.tool_names = tool_names
                self.final_messages = messages
                self.final_limits = limits
                self.sink({'type': 'model_usage', 'available': True,
                           'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}})
                return Decision('finish', {'answer': 'Partial work and remaining checks reported'})
            def decide_with_limits(self, messages, limits):
                raise AssertionError('Finalization must restrict offered tools')
        raw = Model()
        guarded = _BudgetedModel(raw, Counter(), 300, 30)
        guarded.accounted_tokens = 142
        decision = guarded.decide([{'role': 'user', 'content': 'Raw execution history'}])
        self.assertEqual('finish', decision.action)
        self.assertEqual(['finish'], raw.tool_names)
        self.assertNotIn('Raw execution history', json.dumps(raw.final_messages))
        self.assertEqual(['system', 'user'], [message['role'] for message in raw.final_messages])
        self.assertEqual(10, raw.final_limits['max_tokens'])
        self.assertEqual(150, guarded.accounted_tokens)
        self.assertTrue(guarded.finalization_attempted)

    def test_500k_boundary_admits_exact_fit_and_blocks_requests_past_limit(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10

        class Model:
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def decide_with_tools(self, messages, limits, tools):
                return Decision('finish', {'outcome': 'incomplete', 'answer': 'Budget reached'})
            def decide_with_limits(self, messages, limits):
                return Decision('read_file', {'path': 'app.py'})

        for remaining, expected_mode in ((168, 'normal'), (167, 'finalize'),
                                         (148, 'finalize'), (147, None)):
            with self.subTest(remaining=remaining):
                guarded = _BudgetedModel(Model(), Counter(), 500_000, 30)
                guarded.accounted_tokens = 500_000 - remaining
                events = []
                guarded.set_event_sink(events.append)
                if expected_mode is None:
                    with self.assertRaisesRegex(ProviderError, 'compact finish request does not fit'):
                        guarded.decide([])
                    self.assertEqual(0, guarded.calls)
                else:
                    guarded.decide([])
                    request = next(event for event in events if event['type'] == 'evaluation_model_input')
                    self.assertEqual(expected_mode, request['mode'])
                    self.assertLessEqual(request['requestEstimateTokens'] + request['heldFinalizationTokens'],
                                         remaining)
                    self.assertLessEqual(guarded.accounted_tokens, 500_000)

    def test_high_provider_usage_after_retry_is_not_discarded(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10

        class Model:
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def get_run_metadata(self):
                return {'transport_retry': {'max_attempts': 2}}
            def decide_with_tools(self, messages, limits, tools):
                self.sink({'type': 'model_request_attempt', 'outcome': 'failed'})
                self.sink({'type': 'model_usage', 'available': True, 'usage': {'total_tokens': 360}})
                return Decision('finish', {'outcome': 'incomplete', 'answer': 'Budget reached'})
            def decide_with_limits(self, messages, limits):
                raise AssertionError('Expected only the smaller finish request to fit')

        guarded = _BudgetedModel(Model(), Counter(), 500_000, 30)
        guarded.accounted_tokens = 499_680
        with self.assertRaisesRegex(ProviderError, 'exceeded the hard accounting limit'):
            guarded.decide([])
        self.assertEqual(500_188, guarded.accounted_tokens)
        with self.assertRaisesRegex(ProviderError, 'compact finish request does not fit'):
            guarded.decide([])
        self.assertEqual(1, guarded.calls)

    def test_cumulative_budget_failure_is_not_sent_to_provider(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10
        class Model:
            calls = 0
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def get_run_metadata(self):
                return {'transport_retry': {'max_attempts': 1}}
            def decide_with_tools(self, messages, limits, tool_names):
                self.calls += 1
                return Decision('finish', {'answer': 'Done'})
            def decide_with_limits(self, messages, limits):
                self.calls += 1
        raw = Model()
        guarded = _BudgetedModel(raw, Counter(), 100, 30)
        guarded.accounted_tokens = 90
        with self.assertRaisesRegex(ProviderError, 'EVALUATION_TOKEN_BUDGET'):
            guarded.decide([])
        self.assertEqual(0, raw.calls)

    def test_provider_usage_over_accounting_limit_stops_run_with_usage_preserved(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10
        class Model:
            calls = 0
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def get_run_metadata(self):
                return {'transport_retry': {'max_attempts': 1}}
            def decide_with_tools(self, messages, limits, tool_names):
                self.calls += 1
                self.sink({'type': 'model_usage', 'available': True,
                           'usage': {'prompt_tokens': 250, 'completion_tokens': 60,
                                     'total_tokens': 310}})
                return Decision('finish', {'answer': 'Partial result'})
            def decide_with_limits(self, messages, limits):
                self.calls += 1
                return Decision('finish', {'answer': 'Partial result'})
        raw = Model()
        guarded = _BudgetedModel(raw, Counter(), 300, 30)
        guarded.accounted_tokens = 145
        with self.assertRaisesRegex(ProviderError, 'exceeded the hard accounting limit'):
            guarded.decide([])
        self.assertEqual(1, raw.calls)
        self.assertEqual(310, guarded.accounting()['providerUsageSettledTokens'])
        self.assertEqual(455, guarded.accounting()['accountedTokens'])

    def test_harness_reports_budget_and_model_failures_without_repository_tools(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10

        class Model:
            def __init__(self, unexpected_tool):
                self.unexpected_tool = unexpected_tool
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def decide_with_limits(self, messages, limits):
                raise ProviderError('execution provider failed')
            def decide_with_tools(self, messages, limits, names):
                if names != ['finish']:
                    raise AssertionError('Finalization exposed unexpected tools')
                if self.unexpected_tool:
                    return Decision('read_file', {'path': 'app.py'})
                raise ProviderError('report provider failed')

        tools = SimpleNamespace(root=self.source)
        for cap, status, calls, spent, unexpected_tool, reason in (
                (100, 'budget_exhausted', 0, 0, False, 'does not fit'),
                (160, 'finalization_failed', 1, 148, False, 'report provider failed'),
                (160, 'finalization_failed', 1, 148, True, 'did not return finish'),
                (300, 'error', 1, 20, False, 'execution provider failed')):
            with self.subTest(reason=reason):
                guarded = _BudgetedModel(Model(unexpected_tool), Counter(), cap, 30,
                                         task={'taskId': 'fixture', 'problemStatement': 'Change value'})
                agent = Agent(guarded, tools, trace_dir=self.root / 'trace')
                result = agent.run('Change value')
                self.assertEqual(status, result['status'])
                self.assertEqual('incomplete', result['outcome'])
                self.assertEqual(0, result['tool_actions'])
                self.assertEqual(calls, guarded.calls)
                report = result['finalizationReport']
                self.assertIn(reason, report['reason'])
                self.assertEqual(spent, report['accountedTokens'])
                self.assertEqual('fixture', report['state']['taskId'])
                self.assertIsNone(report['state']['latestCheck'])
                self.assertIn('evaluation_finalization_fallback', agent.trace_path.read_text(encoding='utf-8'))

    def test_rejected_finish_with_unfinished_plan_reports_incomplete(self):
        class Counter:
            output_tokens = 10
            input_tokens = 50
            def require(self, messages, tools):
                return 10

        class Model:
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def decide_with_limits(self, messages, limits):
                self.sink({'type': 'model_usage', 'available': True,
                           'usage': {'prompt_tokens': 5, 'completion_tokens': 3}})
                return Decision('update_plan', {'reason': 'Remaining work', 'steps': [{
                    'id': 'verify', 'goal': 'Verify behavior', 'status': 'pending',
                    'evidence_refs': [], 'blocked_reason': ''}]})
            def decide_with_tools(self, messages, limits, names):
                return Decision('finish', {'outcome': 'completed', 'answer': 'Unverified completion'})

        guarded = _BudgetedModel(Model(), Counter(), 175, 30)
        agent = Agent(guarded, SimpleNamespace(root=self.source), trace_dir=self.root / 'trace')
        result = agent.run('Change value')
        self.assertEqual('finalization_failed', result['status'])
        self.assertEqual('incomplete', result['outcome'])
        self.assertIn('Unfinished steps: verify', result['finalizationReport']['reason'])
        self.assertEqual(2, guarded.calls)
        events = [json.loads(line) for line in agent.trace_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual('PLAN_INCONSISTENT', next(e['reason'] for e in events if e['type'] == 'finish_rejected'))
        self.assertFalse(any(e['type'] == 'finish' for e in events))

    def test_scripted_model_runs_production_agent_and_preserves_patch_bytes(self):
        directory = self.prepared()
        baseline = self.report(resolved=False)
        data = json.loads(baseline.read_bytes())
        data[self.row['instance_id']]['tests_status'] = {
            'FAIL_TO_PASS': {'success': [], 'failure': ['test_target']},
            'PASS_TO_PASS': {'success': ['test_control'], 'failure': []}}
        baseline.write_text(json.dumps(data), encoding='utf-8')
        class Counter:
            output_tokens = 32
            input_tokens = 1000
            counter_name = 'offline:fixture'
            def require(self, messages, tools):
                return 100
        class ScriptedModel:
            model = 'offline-scripted'
            calls = 0
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def get_run_metadata(self):
                return {'source': 'offline_script', 'max_retries': 0}
            def decide_with_limits(self, messages, limits):
                self.calls += 1
                self.sink({'type': 'model_request_attempt', 'outcome': 'response_received'})
                self.sink({'type': 'model_usage', 'available': True, 'usage': {'prompt_tokens': 100, 'completion_tokens': 5}})
                if self.calls == 1:
                    return Decision('read_file', {'path': 'app.py', 'start': 1, 'end': 1})
                if self.calls == 2:
                    observation = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                    return Decision('replace_lines', {'snapshot_id': observation['data']['snapshot_id'],
                                    'start': 1, 'end': 1, 'new_text': 'value = 2\n'})
                return Decision('finish', {'answer': 'Updated app.py. No tests run.',
                                'outcome': 'completed', 'evidence': 'Successful replace_lines observation.'})
        model = ScriptedModel()
        with patch('repo_agent.evaluation.runner.TokenBudget', return_value=Counter()), \
                patch('urllib.request.urlopen', side_effect=AssertionError('Offline case must not access network')):
            run_agent(directory, baseline_report=baseline, model=model,
                      max_steps=3, max_total_tokens=10000, max_seconds=30, output_tokens=32)
        attempt = json.loads((directory / 'attempt.json').read_bytes())
        self.assertEqual('agent_reported_finished', attempt['agentStatus'])
        self.assertEqual(3, attempt['modelCalls'])
        self.assertEqual({'inputTokens': 300, 'outputTokens': 15}, attempt['usage'])
        self.assertEqual(3, attempt['tokenAccounting']['providerSettledCalls'])
        self.assertEqual(315, attempt['tokenAccounting']['providerUsageSettledTokens'])
        self.assertEqual(0, attempt['tokenAccounting']['finalAnswerReserveTokens'])
        self.assertEqual('not_run', attempt['officialGrade'])
        self.assertIsNone(attempt['cost'])
        config = json.loads((directory / 'execution-config.json').read_bytes())
        self.assertEqual('injected_model', config['source'])
        grading_command(directory)
        self.assertEqual('value = 1\n', (self.source / 'app.py').read_text())
        trace = next((directory / 'trace').glob('*.jsonl')).read_text(encoding='utf-8')
        self.assertNotIn('HIDDEN_GOLD', trace)
        self.assertNotIn('HIDDEN_TEST_PATCH', trace)
        with self.assertRaises(ValueError), patch('urllib.request.urlopen') as network:
            run_agent(directory, baseline_report=baseline, model=model,
                      max_steps=3, max_total_tokens=10000, max_seconds=30, output_tokens=32)
        network.assert_not_called()

    def test_stubbed_official_process_imports_report_from_pinned_log_layout(self):
        directory = self.prepared()
        self.export(directory)
        original_run = subprocess.run
        grading_calls = []
        def external_process(command, **kwargs):
            if command[0] == 'git':
                return original_run(command, **kwargs)
            if command[1] == '-c':
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({'swebench': '5.0.2'}))
            grading_calls.append(command)
            self.assertEqual('official-runner.py', Path(command[1]).name)
            self.assertTrue((Path(kwargs['cwd']) / 'frozen-grading-input.json').is_file())
            self.assertEqual('1', command[command.index('--max_workers') + 1])
            grading_id = command[command.index('--run_id') + 1]
            log = Path(kwargs['cwd']) / 'logs/run_evaluation' / grading_id / 'coding-agent' / self.row['instance_id']
            log.mkdir(parents=True)
            (log / 'report.json').write_bytes(self.report().read_bytes())
            (log / 'patch.diff').write_bytes((directory / 'candidate.patch').read_bytes())
            (log / 'test_output.txt').write_text('offline evaluator stub', encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, stdout=b'offline evaluator stub', stderr=b'')
        with patch('repo_agent.evaluation.grader.subprocess.run', side_effect=external_process):
            destination = execute_grader(directory)
        self.assertEqual(1, len(grading_calls))
        imported = json.loads((destination / 'imported-grade.json').read_bytes())
        grade = json.loads((Path(imported['gradeDirectory']) / 'grade.json').read_bytes())
        self.assertEqual('pass', grade['status'])
        self.assertFalse(grade['independentlyVerified'])

    def test_failed_grading_process_does_not_import_a_report(self):
        directory = self.prepared()
        self.export(directory)
        original_run = subprocess.run

        def external_process(command, **kwargs):
            if command[0] == 'git':
                return original_run(command, **kwargs)
            if command[1] == '-c':
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({'swebench': '5.0.2'}))
            return subprocess.CompletedProcess(command, 1, stdout=b'', stderr=b'source check failed')

        with patch('repo_agent.evaluation.grader.subprocess.run', side_effect=external_process), \
                patch('repo_agent.evaluation.grader.import_grade') as importing:
            destination = execute_grader(directory)
        importing.assert_not_called()
        result = json.loads((destination / 'ungraded.json').read_bytes())
        self.assertEqual('evaluator_failed', result['reason'])
        self.assertIsNone(result['resolved'])
