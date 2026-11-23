"""Offline tests for request admission and billing telemetry."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from repo_agent.core.coding_budget import BudgetedCodingModel
from repo_agent.core.provider import ContextLimitError, OpenAICompatibleProvider, ProviderError


class FixedTokenCounter:
    input_tokens = 100_000
    output_tokens = 20

    def require(self, messages, tools):
        return 40


def response(usage, *, finish=False):
    action = 'finish' if finish else 'read_file'
    args = {'answer': 'Incomplete', 'outcome': 'incomplete'} if finish else {'path': 'app.py'}
    return io.BytesIO(json.dumps({
        'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'tool_calls': [{
            'id': 'call_fixture', 'type': 'function',
            'function': {'name': action, 'arguments': json.dumps(args)},
        }]}}],
        'usage': usage,
    }).encode())


def adapter(max_retries=0):
    return OpenAICompatibleProvider(model='fixture', base_url='https://provider.invalid/v1',
                                    api_key='fixture', max_retries=max_retries)


class CodingBudgetTests(unittest.TestCase):
    def test_usage_breakdowns_are_subsets_and_reservation_matches_request(self):
        guard = BudgetedCodingModel(adapter(), FixedTokenCounter(), 500_000, 30)
        events = []
        guard.set_event_sink(events.append)
        usage = {'prompt_tokens': 37, 'completion_tokens': 9, 'total_tokens': 46,
                 'prompt_tokens_details': {'cached_tokens': 21},
                 'completion_tokens_details': {'reasoning_tokens': 6}}
        with patch('urllib.request.urlopen', return_value=response(usage)) as send:
            guard.decide_with_limits([{'role': 'user', 'content': 'fixture'}], {'max_tokens': 11})
        self.assertEqual(11, json.loads(send.call_args.args[0].data)['max_tokens'])
        admitted = next(e for e in events if e['type'] == 'evaluation_model_input')
        settled = next(e for e in events if e['type'] == 'evaluation_budget_settlement')
        self.assertEqual(51, admitted['inFlightReservedTokens'])
        self.assertEqual(admitted['budgetRequestId'], settled['budgetRequestId'])
        self.assertEqual(46, settled['accountedTokens'])
        self.assertEqual(0, guard.accounting()['inFlightReservedTokens'])
        self.assertEqual({'inputTokens': 37, 'outputTokens': 9, 'cachedTokens': 21, 'reasoningTokens': 6},
                         guard.usage)

    def test_retry_accounts_for_failed_attempt_and_success_without_charging_unused_slot(self):
        guard = BudgetedCodingModel(adapter(max_retries=2), FixedTokenCounter(), 500_000, 30)
        with patch('urllib.request.urlopen', side_effect=[
                TimeoutError('unknown remote usage'),
                response({'prompt_tokens': 37, 'completion_tokens': 9, 'total_tokens': 46}),
        ]) as send, patch('repo_agent.core.provider.time.sleep'):
            guard.decide([])
        self.assertEqual(2, send.call_count)
        self.assertEqual(180, guard.reserved)
        self.assertEqual(106, guard.accounted_tokens)
        self.assertEqual(1, guard.accounting()['unknownUsageAttempts'])
        self.assertEqual(46, guard.accounting()['providerUsageLowerBoundTokens'])

    def test_http_failure_releases_unattempted_holds_and_local_rejection_costs_zero(self):
        guard = BudgetedCodingModel(adapter(max_retries=2), FixedTokenCounter(), 500_000, 30)
        with patch('urllib.request.urlopen', side_effect=HTTPError(
                'https://provider.invalid', 401, 'fixture', {}, None)) as send:
            with self.assertRaises(ProviderError):
                guard.decide([])
            self.assertEqual(1, send.call_count)
        self.assertEqual(60, guard.accounted_tokens)
        self.assertEqual(1, guard.accounting()['unknownUsageAttempts'])
        with patch('urllib.request.urlopen') as send, self.assertRaises(ContextLimitError):
            guard.decide_with_limits([], {'max_input_bytes': 1})
        send.assert_not_called()
        self.assertEqual(60, guard.accounted_tokens)
        self.assertEqual(0, guard.accounting()['inFlightReservedTokens'])

    def test_same_ledger_allows_only_one_request_to_hold_budget(self):
        guard = BudgetedCodingModel(adapter(), FixedTokenCounter(), 500_000, 30)
        guard.accounted_tokens = 499_750
        entered, release = Event(), Event()

        def send(request, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Timed out waiting for concurrent admission check')
            return response({'prompt_tokens': 38, 'completion_tokens': 19, 'total_tokens': 57})

        with (patch('urllib.request.urlopen', side_effect=send) as network,
              ThreadPoolExecutor(max_workers=2) as pool):
            first = pool.submit(guard.decide, [])
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(60, guard.accounting()['inFlightReservedTokens'])
                with self.assertRaisesRegex(ProviderError, 'EVALUATION_REQUEST_IN_FLIGHT'):
                    pool.submit(guard.decide, []).result(timeout=5)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(1, network.call_count)
        self.assertEqual(499_807, guard.accounted_tokens)

    def test_ledger_isolation_when_tasks_share_a_provider(self):
        provider = adapter()
        left = BudgetedCodingModel(provider, FixedTokenCounter(), 500_000, 30)
        right = BudgetedCodingModel(provider, FixedTokenCounter(), 500_000, 30)
        waiting = Barrier(2)

        def send(request, **kwargs):
            messages = json.loads(request.data)['messages']
            used = int(messages[0]['content'])
            waiting.wait(timeout=5)
            return response({'prompt_tokens': used, 'completion_tokens': 2})

        with patch('urllib.request.urlopen', side_effect=send), ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(guard.decide, [{'role': 'user', 'content': str(amount)}])
                     for guard, amount in ((left, 17), (right, 23))]
            for call in calls:
                call.result(timeout=5)
        self.assertEqual((19, 25), (left.accounted_tokens, right.accounted_tokens))

    def test_compact_finish_settles_overrun_and_blocks_later_dispatch(self):
        guard = BudgetedCodingModel(adapter(), FixedTokenCounter(), 500_000, 30)
        guard.accounted_tokens = 499_805
        with patch('urllib.request.urlopen', return_value=response(
                {'prompt_tokens': 185, 'completion_tokens': 20}, finish=True)) as send:
            with self.assertRaisesRegex(ProviderError, 'exceeded'):
                guard.decide([{'role': 'user', 'content': 'FULL EXECUTION HISTORY'}])
            body = json.loads(send.call_args.args[0].data)
            self.assertEqual(['system', 'user'], [message['role'] for message in body['messages']])
            self.assertNotIn('FULL EXECUTION HISTORY', json.dumps(body))
            self.assertEqual(['finish'], [tool['function']['name'] for tool in body['tools']])
            self.assertEqual(500_010, guard.accounted_tokens)
            with self.assertRaisesRegex(ProviderError, 'does not fit'):
                guard.decide([])
        self.assertEqual(1, send.call_count)
