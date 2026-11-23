"""Offline streaming protocol regressions; no model or server startup."""
import io
import json
import unittest
from unittest.mock import patch

from repo_agent.core.provider import OpenAICompatibleProvider, ProviderError, TextReply, ToolCallReply
from repo_agent.core.decisions import TOOL_DEFINITIONS


def frame(delta=None, *, finish=None, usage=None):
    value = {'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}
    if usage is not None:
        value['usage'] = usage
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode('utf-8')


class ProviderStreamTests(unittest.TestCase):
    def provider(self):
        with patch('repo_agent.core.provider.read_dotenv', return_value={}):
            return OpenAICompatibleProvider(model='fixture', base_url='https://fixture.invalid/v1', api_key='')

    def test_text_is_emitted_before_response_finishes_and_usage_survives(self):
        events = []
        provider = self.provider()
        provider.set_event_sink(events.append)
        class ObservedResponse(io.BytesIO):
            def __next__(self):
                if self.tell() > len(frame({'content': '你好'})):
                    assert any(e['type'] == 'model_text' for e in events)
                return super().__next__()
        raw = (frame({'content': '你好'}) + frame({'content': '世界'})
               + frame(finish='stop', usage={'prompt_tokens': 9, 'completion_tokens': 4})
               + b'data: [DONE]\n\n')
        with patch('repo_agent.core.provider.urllib.request.urlopen', return_value=ObservedResponse(raw)) as send:
            result = provider.respond_stream([{'role': 'user', 'content': '问'}])
        self.assertIsInstance(result, TextReply)
        self.assertEqual('你好世界', result.text)
        self.assertTrue(json.loads(send.call_args.args[0].data)['stream'])
        self.assertTrue(any(e['type'] == 'model_usage' and e['usage'].get('prompt_tokens') == 9 for e in events))

    def test_split_tool_arguments_are_assembled_before_validation(self):
        raw = (frame({'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function',
                                      'function': {'name': 'read_file', 'arguments': '{"path":'}}]})
               + frame({'tool_calls': [{'index': 0, 'function': {'arguments': '"a.py"}'}}]})
               + frame(finish='tool_calls') + b'data: [DONE]\n\n')
        with patch('repo_agent.core.provider.urllib.request.urlopen', return_value=io.BytesIO(raw)):
            result = self.provider().respond_stream([], tools=TOOL_DEFINITIONS)
        self.assertIsInstance(result, ToolCallReply)
        self.assertEqual({'path': 'a.py'}, result.decision.args)

    def test_incomplete_stream_is_not_a_completed_answer_or_retried(self):
        raw = frame({'content': '未完成'})
        with patch('repo_agent.core.provider.urllib.request.urlopen', return_value=io.BytesIO(raw)) as send:
            with self.assertRaises(ProviderError):
                self.provider().respond_stream([])
        self.assertEqual(1, send.call_count)
