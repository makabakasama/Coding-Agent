"""Focused offline acceptance of cross-file delivery and decision feedback."""
from copy import deepcopy
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.repositories.task_store import TaskStore
from repo_agent.backend.services.task_execution_tools import TaskRepoTools
from repo_agent.backend.services.task_service import TaskService
from repo_agent.core.agent import Agent
from repo_agent.core.context import latest_tool_result
from repo_agent.core.decisions import Decision, parse_tool_decision
from tests.test_tasks import http, serving


CHECK = 'tests/test_delivery.py'
MODULE = 'def parse_number(value):\n    return int(value)\n'
CALLER = 'from parsing import parse_number\n\ndef parse(value):\n    return parse_number(value)\n'


def plan_step(identifier, goal, status='pending', refs=(), blocked=''):
    return {'id': identifier, 'goal': goal, 'status': status,
            'evidence_refs': list(refs), 'blocked_reason': blocked}


def update(steps, reason='Update from observed work'):
    return Decision('update_plan', {'steps': steps, 'reason': reason})


def runtime(messages):
    return json.loads(messages[-1]['content'])['runtime_state']


class ScenarioModel:
    def __init__(self, decisions):
        self.decisions = decisions
        self.inputs = []

    def decide(self, messages):
        self.inputs.append(deepcopy(messages))
        decision = self.decisions[len(self.inputs) - 1]
        if callable(decision):
            decision = decision(messages)
        return parse_tool_decision(decision.assistant_message)


class ProgressAcceptanceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'source'
        self.source.mkdir()
        self.original = b'def parse(value):\n    return value\n'
        (self.source / 'caller.py').write_bytes(self.original)
        (self.source / 'tests').mkdir()
        (self.source / CHECK).write_bytes(
            b'import pytest\nfrom caller import parse\n\n'
            b'def test_number():\n    assert parse("12") == 12\n\n'
            b'@pytest.mark.parametrize("value", ["", "invalid"])\n'
            b'def test_invalid(value):\n    with pytest.raises(ValueError):\n        parse(value)\n')
        self.check_bytes = (self.source / CHECK).read_bytes()
        self.contract = {'kind': 'add_feature', 'allowedPaths': ['caller.py', 'parsing.py'],
                         'allowTests': True, 'checkTargets': [CHECK],
                         'workspaceMode': 'direct', 'sourceKind': 'directory'}

    def save_case(self, result, model, trace, delivery=None):
        output = os.environ.get('CODING_AGENT_ACCEPTANCE_OUTPUT')
        if not output:
            return
        target = Path(output) / self._testMethodName
        target.mkdir(parents=True, exist_ok=False)
        for name, value in [('result.json', result), ('model-inputs.json', model.inputs)]:
            (target / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        shutil.copyfile(trace, target / 'trace.jsonl')
        patch_text = ''.join(difflib.unified_diff(self.original.decode().splitlines(keepends=True),
                                               (self.source / 'caller.py').read_text().splitlines(keepends=True),
                                               fromfile='a/caller.py', tofile='b/caller.py'))
        if (self.source / 'parsing.py').exists():
            patch_text += ''.join(difflib.unified_diff([], (self.source / 'parsing.py').read_text().splitlines(keepends=True),
                                                     fromfile='/dev/null', tofile='b/parsing.py'))
        (target / 'changes.diff').write_text(patch_text, encoding='utf-8')
        (target / 'frozen-check.py').write_bytes(self.check_bytes)
        (target / 'check.sha256').write_text(hashlib.sha256(self.check_bytes).hexdigest(), encoding='ascii')
        if (self.directory / 'checks').exists():
            shutil.copytree(self.directory / 'checks', target / 'checks')
        if delivery is not None:
            shutil.copytree(delivery, target / 'delivery')

    def assert_inputs(self, model, budget):
        for index, messages in enumerate(model.inputs):
            self.assertEqual(model.inputs[0][:2], messages[:2])
            tails = [message for message in messages if message['role'] == 'user'
                     and message['content'].startswith('{"runtime_state":')]
            self.assertEqual(1, len(tails))
            self.assertIs(tails[0], messages[-1])
            state = runtime(messages)
            self.assertEqual(index, state['used_decisions'])
            self.assertEqual(budget - index, state['remaining_decisions'])
            pending = set()
            for message in messages[2:-1]:
                if message['role'] == 'assistant':
                    pending.update(call['id'] for call in message.get('tool_calls', []))
                if message['role'] == 'tool':
                    self.assertIn(message['tool_call_id'], pending)
                    pending.remove(message['tool_call_id'])
            self.assertEqual(set(), pending)
        self.assertEqual(self.check_bytes, (self.source / CHECK).read_bytes())

    def run_case(self, decisions):
        model = ScenarioModel(decisions)
        tools = TaskRepoTools(self.source, contract=self.contract, test_python=Path(sys.executable),
                              test_log_dir=self.directory / 'checks', test_junit=True,
                              test_env={'PYTHONPATH': str(self.source), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'})
        agent = Agent(model, tools, trace_dir=self.directory / 'trace', max_steps=len(decisions))
        with patch('urllib.request.urlopen', side_effect=AssertionError('Offline acceptance only')):
            result = agent.run('Deliver the frozen cross-file parsing requirement')
        self.save_case(result, model, agent.trace_path)
        self.assert_inputs(model, len(decisions))
        return result, model

    def test_cross_file_delivery_through_saved_task_and_http_results(self):
        refs = {}
        initial = [plan_step('module', 'Create parsing module', 'in_progress'),
                   plan_step('caller', 'Update caller'), plan_step('check', 'Pass frozen checks')]

        def read_caller(messages):
            observation = latest_tool_result(messages)
            self.assertTrue(observation['data']['applied'])
            refs['module'] = observation['observation_id']
            return Decision('read_file', {'path': 'caller.py'})

        def edit_caller(messages):
            return Decision('replace_lines', {'snapshot_id': latest_tool_result(messages)['data']['snapshot_id'],
                                              'start': 1, 'end': 2, 'new_text': CALLER})

        def check(messages):
            refs['caller'] = latest_tool_result(messages)['observation_id']
            return Decision('run_tests', {'target': CHECK})

        def complete(messages):
            observation = latest_tool_result(messages)
            self.assertTrue(observation['data']['passed'])
            refs['check'] = observation['observation_id']
            return update([plan_step(item['id'], item['goal'], 'completed', [refs[item['id']]]) for item in initial])

        def finish(messages):
            self.assertEqual(2, runtime(messages)['progress']['revision'])
            self.assertEqual([], runtime(messages)['progress']['remaining_step_ids'])
            return Decision('finish', {'answer': 'Delivered parsing module and caller with frozen checks.'})

        model = ScenarioModel([update(initial), Decision('create_file', {'path': 'parsing.py', 'content': MODULE}),
                               read_caller, edit_caller, check, complete, finish])
        service = TaskService(TaskStore(self.directory / 'tasks'), TaskProjects(self.directory / 'projects.json'),
                              workspace_root=self.directory / 'workspaces', model_factory=lambda: model)
        record = service.create({'targetPath': str(self.source), 'instruction': 'Extract integer parsing into parsing.py and update caller.py',
                                 'allowedPaths': ['caller.py', 'parsing.py'], 'allowTests': True,
                                 'checkTargets': [CHECK], 'maxSteps': 7})
        task_id = record['taskId']
        with patch('urllib.request.urlopen', side_effect=AssertionError('Offline acceptance only')):
            service.execution.run_claimed(task_id, uuid.uuid4().hex)
        result = service.execution.result(task_id)
        delivery = service.store.directory(task_id) / 'execution'
        trace = next((delivery / 'trace').glob('*.jsonl'))
        self.save_case(result, model, trace, delivery)
        self.assert_inputs(model, 7)
        self.assertEqual('completed', result['completion']['status'], result)
        self.assertEqual('passed', result['review']['status'])
        self.assertEqual('passed', result['finalChecks']['status'])
        self.assertEqual(['caller.py', 'parsing.py'], result['review']['changedPaths'])
        self.assertEqual([], result['progress']['remaining_step_ids'])
        self.assertFalse(result['independentlyVerified'])
        patch_bytes = (delivery / 'candidate.patch').read_bytes()
        self.assertIn(b'parsing.py', patch_bytes)
        self.assertIn(b'from parsing import parse_number', patch_bytes)
        with serving(service) as port:
            state = http(port, 'GET', f'/api/tasks/{task_id}/execution')[2]
            self.assertEqual(2, state['progress']['revision'])
            events = http(port, 'GET', f'/api/tasks/{task_id}/execution/events?limit=100')[2]['events']
            self.assertTrue(any(event['type'] == 'progress_updated' for event in events))
            self.assertEqual(result, http(port, 'GET', f'/api/tasks/{task_id}/execution/result')[2])

    def test_tool_failure_is_visible_before_blocking_plan_and_delivery(self):
        def block(messages):
            observation = latest_tool_result(messages)
            self.assertFalse(observation['ok'])
            self.assertIn('FileExistsError', observation['error'])
            return update([plan_step('module', 'Create module', 'blocked', [observation['observation_id']], 'Target already exists')])

        result, model = self.run_case([update([plan_step('module', 'Create module')]),
                                      Decision('create_file', {'path': 'caller.py', 'content': 'overwrite'}), block,
                                      Decision('finish', {'answer': 'Blocked', 'outcome': 'blocked', 'evidence': 'Existing target prevents creation'})])
        self.assertEqual('blocked', result['outcome'])
        self.assertEqual('blocked', runtime(model.inputs[-1])['progress']['steps'][0]['status'])
        self.assertEqual(self.original, (self.source / 'caller.py').read_bytes())

    def test_expired_check_rejection_enters_next_decision_and_can_be_repaired(self):
        (self.source / 'caller.py').write_bytes(b'def parse(value):\n    return int(value)\n')
        check_refs = []

        def mark_complete(messages):
            observation = latest_tool_result(messages)
            self.assertTrue(observation['data']['passed'])
            check_refs.append(observation['observation_id'])
            return update([plan_step('verify', 'Verify current source', 'completed', [check_refs[-1]])])

        def edit(messages):
            return Decision('replace_lines', {'snapshot_id': latest_tool_result(messages)['data']['snapshot_id'],
                                              'start': 2, 'end': 2, 'new_text': '    return int(value.strip())\n'})

        def recheck(messages):
            self.assertFalse(latest_tool_result(messages)['ok'])
            self.assertEqual(check_refs, runtime(messages)['progress']['stale_evidence_refs'])
            self.assertIsNone(runtime(messages)['progress']['last_test_passed'])
            return Decision('run_tests', {'target': CHECK})

        result, _ = self.run_case([update([plan_step('verify', 'Verify current source')]),
                                  Decision('run_tests', {'target': CHECK}), mark_complete,
                                  Decision('read_file', {'path': 'caller.py'}), edit,
                                  Decision('finish', {'answer': 'Premature completion'}), recheck, mark_complete,
                                  Decision('finish', {'answer': 'Rechecked current source'})])
        self.assertEqual('completed', result['outcome'])
        self.assertEqual([], result['progress']['stale_evidence_refs'])
        self.assertTrue(result['verified_local_change'])

    def test_early_finish_feedback_preserves_unfinished_work(self):
        def explain(messages):
            self.assertIn('Unfinished steps', latest_tool_result(messages)['error'])
            self.assertEqual(['module'], runtime(messages)['progress']['remaining_step_ids'])
            return Decision('finish', {'answer': 'Module exists, plan remains unresolved', 'outcome': 'incomplete',
                                       'evidence': 'Implementation does not establish all acceptance evidence'})

        result, _ = self.run_case([update([plan_step('module', 'Create and verify module')]),
                                  Decision('create_file', {'path': 'parsing.py', 'content': MODULE}),
                                  Decision('finish', {'answer': 'Premature completion'}), explain])
        self.assertEqual('incomplete', result['outcome'])
        self.assertEqual(['module'], result['progress']['remaining_step_ids'])

    def test_budget_exhaustion_retains_plan_and_last_decision_budget(self):
        result, model = self.run_case([update([plan_step('module', 'Create module')]),
                                      Decision('read_file', {'path': 'caller.py'})])
        self.assertEqual('step_limit', result['status'])
        self.assertEqual(0, result['remaining_decisions'])
        self.assertEqual(1, runtime(model.inputs[-1])['remaining_decisions'])
        self.assertEqual(['module'], result['progress']['remaining_step_ids'])

    def test_invalid_evidence_feedback_does_not_commit_plan_revision(self):
        def explain(messages):
            self.assertFalse(latest_tool_result(messages)['ok'])
            self.assertEqual(1, runtime(messages)['progress']['revision'])
            return Decision('finish', {'answer': 'Evidence unresolved', 'outcome': 'incomplete',
                                       'evidence': 'The reference is not an observation from this run'})

        result, _ = self.run_case([update([plan_step('module', 'Create module')]),
                                  update([plan_step('module', 'Create module', 'completed', ['obs_other_run'])]), explain])
        self.assertEqual('incomplete', result['outcome'])
        self.assertEqual('pending', result['progress']['steps'][0]['status'])

    def test_model_error_retains_last_plan_and_remaining_budget(self):
        def fail(messages):
            self.assertEqual(1, runtime(messages)['progress']['revision'])
            raise RuntimeError('Synthetic provider failure')

        result, _ = self.run_case([update([plan_step('module', 'Create module')]), fail,
                                  Decision('finish', {'answer': 'Never reached'})])
        self.assertEqual('error', result['status'])
        self.assertIn('RuntimeError: Synthetic provider failure', result['reason'])
        self.assertEqual(1, result['remaining_decisions'])
        self.assertEqual(['module'], result['progress']['remaining_step_ids'])
