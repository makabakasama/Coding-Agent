"""Regression cases for empty retries and stable prefixes; no Provider needed."""
from copy import deepcopy
import unittest

from repo_agent.core.chat_context import build_chat_context


class ChatContextTests(unittest.TestCase):
    def entry(self, number, *, status='failed', content='同一个问题', role='user'):
        return {
            'message': {'conversationId': 'conversation', 'turnId': f'turn-{number}',
                        'messageId': f'message-{number}-{role}', 'seq': number,
                        'role': role, 'content': content},
            'turnInputSeq': number, 'turnStatus': status, 'stopReason': 'PROVIDER_ERROR',
            'policySnapshot': {'workspaceRef': 'project', 'allowedTools': []},
        }

    def turn(self, history):
        last = history[-1]
        return {'conversationId': 'conversation', 'turnId': last['message']['turnId'],
                'inputMessageId': last['message']['messageId'],
                'contextCutoffSeq': last['turnInputSeq'],
                'policySnapshot': last['policySnapshot']}

    def test_empty_retries_collapse_without_mutating_history(self):
        history = [self.entry(n) for n in range(1, 5)] + [self.entry(5, status='running')]
        original = deepcopy(history)
        messages, manifest = build_chat_context(self.turn(history), history)
        self.assertEqual(1, sum(m['content'] == '同一个问题' for m in messages))
        self.assertEqual(4, len(manifest['collapsedRetries']))
        self.assertEqual(original, history)
        self.assertEqual('同一个问题', messages[-1]['content'])

    def test_evidence_uncertain_status_and_permission_changes_survive(self):
        for changes in ({'programObservations': [{'observationId': 'read-1'}]},
                        {'executionEvidence': {'taskId': 'task'}},
                        {'turnStatus': 'unknown'}, {'turnStatus': 'completed'},
                        {'policySnapshot': {'workspaceRef': 'other'}},
                        {'artifacts': {'reply': {'path': 'reply.json'}}}):
            with self.subTest(changes=changes):
                history = [{**self.entry(1), **changes}, self.entry(2, status='running')]
                _, manifest = build_chat_context(self.turn(history), history)
                self.assertEqual([], manifest['collapsedRetries'])
        history = [self.entry(1), self.entry(2, status='running', content='新的问题')]
        _, manifest = build_chat_context(self.turn(history), history)
        self.assertEqual([], manifest['collapsedRetries'])

    def test_dynamic_fields_do_not_change_history_prefix(self):
        history = [self.entry(1, status='completed'),
                   self.entry(1, status='completed', role='assistant', content='历史回答'),
                   self.entry(2, status='running', content='新问题')]
        turn = self.turn(history)
        first, original = build_chat_context(turn, history)
        changed = deepcopy(turn)
        changed['policySnapshot']['workspaceRef'] = 'other'
        second, updated = build_chat_context(changed, history, omitted_turn_count=2)
        count = original['stablePrefixMessages']
        self.assertEqual(first[:count], second[:count])
        self.assertEqual(original['stablePrefixSha256'], updated['stablePrefixSha256'])
        self.assertNotEqual(first[-2], second[-2])
        self.assertEqual(first[-1], second[-1])
