"""SSE replay checks using in-memory handlers; no server or database."""
import io
import json
import unittest

from repo_agent.backend.routers.chat_http import _stream_events


class ChatStreamTransportTests(unittest.TestCase):
    def test_cursor_replays_unicode_event_and_finishes_after_terminal_drain(self):
        class Handler:
            wfile = io.BytesIO()
            headers_sent = {}
            def send_response(self, status):
                self.status = status
            def send_header(self, key, value):
                self.headers_sent[key] = value
            def end_headers(self):
                pass
        class Chat:
            cursors = []
            def events(self, cid, tid, *, after, limit):
                self.cursors.append(after)
                events = ([{'type': 'model_text', 'seq': 8, 'turnId': tid, 'text': '你好'}]
                          if after == 7 else [])
                return {'events': events, 'hasMore': False, 'nextAfter': 8}
            def turn(self, cid, tid):
                return {'status': 'completed'}
        handler, chat = Handler(), Chat()
        _stream_events(handler, chat, 'conversation', 'turn', 7)
        text = handler.wfile.getvalue().decode('utf-8')
        self.assertEqual('text/event-stream; charset=utf-8', handler.headers_sent['Content-Type'])
        self.assertIn('id: 8\n', text)
        self.assertIn('event: stream_done\n', text)
        payload = next(line[6:] for line in text.splitlines() if line.startswith('data: '))
        self.assertEqual('你好', json.loads(payload)['text'])
        self.assertEqual([7, 8], chat.cursors)
