"""SSE replay checks using in-memory handlers; no server or database."""
import io
import json
import unittest
from unittest.mock import Mock, patch

from repo_agent.backend.routers.chat_http import _stream_events
from repo_agent.backend.schemas.task_contract import TaskError


class MemoryHandler:
    auth = None

    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers_sent = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass


class ChatStreamTransportTests(unittest.TestCase):
    def test_cursor_replays_unicode_event_and_finishes_after_terminal_drain(self):
        class Chat:
            cursors = []
            def events(self, cid, tid, *, after, limit):
                self.cursors.append(after)
                events = ([{'type': 'model_text', 'seq': 8, 'turnId': tid, 'text': '你好'}]
                          if after == 7 else [])
                return {'events': events, 'hasMore': False, 'nextAfter': 8}
            def turn(self, cid, tid):
                return {'status': 'completed'}
        handler, chat = MemoryHandler(), Chat()
        _stream_events(handler, chat, 'conversation', 'turn', 7)
        text = handler.wfile.getvalue().decode('utf-8')
        self.assertEqual('text/event-stream; charset=utf-8', handler.headers_sent['Content-Type'])
        self.assertIn('id: 8\n', text)
        self.assertIn('event: stream_done\n', text)
        payload = next(line[6:] for line in text.splitlines() if line.startswith('data: '))
        self.assertEqual('你好', json.loads(payload)['text'])
        self.assertEqual([7, 8], chat.cursors)

    def test_stream_stops_before_next_page_when_session_check_fails(self):
        for status, code in ((401, 'SESSION_REQUIRED'), (503, 'SESSION_STORE_UNAVAILABLE')):
            with self.subTest(status=status):
                handler = MemoryHandler()
                chat = Mock()
                chat.events.side_effect = [
                    {'events': [{'seq': 1, 'text': 'first page'}], 'hasMore': True},
                    {'events': [{'seq': 2, 'text': 'private second page'}], 'hasMore': False},
                ]
                checks = [None, None, TaskError(status, code, 'Injected authentication failure')]
                with patch('repo_agent.backend.routers.chat_http.require_admin', side_effect=checks):
                    _stream_events(handler, chat, 'conversation', 'turn', 0)
                body = handler.wfile.getvalue().decode('utf-8')
                self.assertEqual(200, handler.status)
                self.assertIn('first page', body)
                self.assertNotIn('private second page', body)
                self.assertNotIn('event: stream_done', body)
                self.assertTrue(handler.close_connection)
                self.assertEqual(2, chat.events.call_count)
