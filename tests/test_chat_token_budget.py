"""Token budget policy checks; no model or network is required."""
import unittest
from unittest.mock import patch

from repo_agent.core.provider import ContextLimitError, ProviderError
from repo_agent.core.provider import TextReply, ToolCallReply
from repo_agent.core.decisions import Decision
from repo_agent.core.agent import Agent
from repo_agent.backend.services.chat_execution import _BudgetedModel
from repo_agent.core.token_budget import TokenBudget


class ChatTokenBudgetTests(unittest.TestCase):
    def test_reserve_and_tool_result_are_counted_on_every_request(self):
        with patch.object(TokenBudget, '_load_counter',
                          return_value=(lambda text: list(text), 'fixture:characters')):
            budget = TokenBudget('fixture', context_tokens=200, output_tokens=20,
                                 reserve_tokens=20)
        first = [{'role': 'user', 'content': '读文件'}]
        used = budget.require(first, [])
        self.assertEqual(160, budget.input_tokens)
        self.assertEqual(used, budget.manifest(used)['estimatedInputTokens'])
        observed = first + [{'role': 'tool', 'content': 'x' * 200}]
        with self.assertRaises(ContextLimitError):
            budget.require(observed, [])

    def test_unknown_model_needs_explicit_tokenizer(self):
        with self.assertRaises(ProviderError):
            TokenBudget('unmapped-provider-model', context_tokens=1000,
                        output_tokens=100, reserve_tokens=100)

    def test_chat_can_answer_after_more_than_eight_tool_decisions(self):
        class Counter:
            input_tokens = 100_000
            output_tokens = 10
            counter_name = 'fixture'

            def count(self, messages, tools):
                return 100

            def require(self, messages, tools):
                return 100

        class Model:
            calls = 0

            def respond(self, messages, *, tools, response_limits):
                self.calls += 1
                if self.calls <= 9:
                    return ToolCallReply(Decision('list_files', {'subdir': '.', 'limit': 1}))
                return TextReply('读取后回答', {'role': 'assistant', 'content': '读取后回答'})

        class Tools:
            def execute(self, action, args):
                return {'ok': True, 'data': {'files': []}}

        raw = Model()
        guarded = _BudgetedModel(raw, Counter(), lambda used, total: None,
                                 total_tokens=100_000, max_seconds=600)
        reply, decisions = Agent.run_conversation(
            guarded, [{'role': 'user', 'content': '列出项目'}], tools=Tools(),
            response_limits={'max_tokens': 10})
        self.assertEqual('读取后回答', reply.text)
        self.assertEqual(10, decisions)


if __name__ == '__main__':
    unittest.main()
