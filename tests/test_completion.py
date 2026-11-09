"""Injected decisions verify completion policy without model network access."""
import json
from pathlib import Path
import tempfile
import unittest

from repo_agent.core.agent import Agent
from repo_agent.core.decisions import Decision
from tests.test_task_execution import ScriptedModel


class Tools:
    def __init__(self, root, kind):
        self.root = root
        self.contract = {'kind': kind}

    def execute(self, action, args):
        return {'ok': True, 'data': {'applied': True}}


class CompletionTest(unittest.TestCase):
    def run_agent(self, decisions, kind='add_feature'):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = Agent(ScriptedModel(decisions), Tools(root, kind), trace_dir=root, max_steps=len(decisions))
            result = agent.run('Implement the requested feature')
            events = [json.loads(line) for line in agent.trace_path.read_text(encoding='utf-8').splitlines()]
        return result, events

    def test_premature_finish_is_rejected_then_implementation_can_continue(self):
        result, events = self.run_agent([
            Decision('finish', {'answer': 'Will implement later'}),
            Decision('replace_text', {'path': 'app.py', 'old_text': '1', 'new_text': '2'}),
            Decision('finish', {'answer': 'Implemented'}),
        ])
        self.assertEqual('completed', result['outcome'])
        self.assertEqual(3, result['steps'])
        self.assertEqual(1, len([e for e in events if e['type'] == 'finish_rejected']))

    def test_repeated_empty_finish_uses_existing_budget(self):
        result, _ = self.run_agent([Decision('finish', {'answer': 'Done'}) for _ in range(2)])
        self.assertEqual('step_limit', result['status'])

    def test_explicit_nonimplementation_outcomes_require_evidence(self):
        for outcome in ('blocked', 'incomplete', 'already_satisfied'):
            with self.subTest(outcome=outcome):
                result, _ = self.run_agent([
                    Decision('finish', {'answer': 'Explanation', 'outcome': outcome}),
                    Decision('finish', {'answer': 'Explanation', 'outcome': outcome,
                                        'evidence': 'app.py was inspected. Specific remaining work or existing behavior.'}),
                ])
                self.assertEqual(outcome, result['outcome'])
                self.assertEqual(2, result['steps'])

    def test_read_only_analysis_can_finish_without_an_edit(self):
        result, _ = self.run_agent([Decision('finish', {'answer': 'Analysis'})], kind='read_code')
        self.assertEqual('agent_reported_finished', result['status'])
