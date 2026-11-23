"""Bounded HTTP teardown after rejecting a request with an unread body."""
from functools import partial
from http.client import HTTPResponse
from pathlib import Path
import select
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from repo_agent.backend.routers.task_http import TaskHandler, TaskHTTPServer


class HTTPShutdownTest(unittest.TestCase):
    def server(self):
        server = TaskHTTPServer.__new__(TaskHTTPServer)
        server.close_request = Mock()
        return server

    def test_shutdown_drains_before_final_close(self):
        server = self.server()
        request = Mock(spec=socket.socket)
        order = []
        chunks = iter([b'body', b''])

        def receive(count):
            order.append('read')
            return next(chunks)

        request.shutdown.side_effect = lambda how: order.append('half_close')
        request.recv.side_effect = receive
        server.close_request.side_effect = lambda connection: order.append('close')
        server.shutdown_request(request)
        self.assertEqual(['half_close', 'read', 'read', 'close'], order)
        request.shutdown.assert_called_once_with(socket.SHUT_WR)

    def test_shutdown_has_a_byte_budget(self):
        server = self.server()
        request = Mock(spec=socket.socket)
        request.recv.side_effect = lambda count: b'x' * count
        with patch('time.monotonic', return_value=100):
            server.shutdown_request(request)
        requested = [call.args[0] for call in request.recv.call_args_list]
        self.assertGreater(len(requested), 0)
        self.assertEqual(65537, sum(requested))
        self.assertLessEqual(max(requested), 8192)
        server.close_request.assert_called_once_with(request)

    def test_shutdown_has_an_overall_deadline(self):
        server = self.server()
        request = Mock(spec=socket.socket)
        request.recv.return_value = b'x'
        with patch('time.monotonic', side_effect=[100, 100, 100.1, 100.25]):
            server.shutdown_request(request)
        self.assertEqual(2, request.recv.call_count)
        self.assertAlmostEqual(.25, request.settimeout.call_args_list[0].args[0])
        self.assertAlmostEqual(.15, request.settimeout.call_args_list[1].args[0])
        server.close_request.assert_called_once_with(request)

    def test_peer_disconnect_and_read_timeout_still_close(self):
        for operation, error in [('shutdown', OSError('disconnected')),
                                 ('recv', TimeoutError('no more body')),
                                 ('recv', ConnectionResetError('peer reset'))]:
            with self.subTest(operation=operation, error=type(error).__name__):
                server = self.server()
                request = Mock(spec=socket.socket)
                getattr(request, operation).side_effect = error
                server.shutdown_request(request)
                server.close_request.assert_called_once_with(request)


class HTTPResponseTeardownTest(unittest.TestCase):
    def exercise(self, body, *, origin=None, content_type='application/json', length=None, half_close=True):
        headers_parsed = threading.Event()
        upload_sent = threading.Event()
        closed = threading.Event()
        facts = {'created': []}

        class Service:
            def create(self, value):
                facts['created'].append(value)
                return {'taskId': 'a' * 32}

        class Handler(TaskHandler):
            def _dispatch(self):
                headers_parsed.set()
                if not upload_sent.wait(3):
                    raise RuntimeError('Upload synchronization failed')
                if body:
                    ready, _, _ = select.select([self.connection], [], [], 3)
                    if not ready:
                        raise RuntimeError('Upload did not arrive')
                return super()._dispatch()

        class Server(TaskHTTPServer):
            def close_request(self, request):
                ready, _, _ = select.select([request], [], [], 0)
                facts['unread_at_close'] = bool(ready and request.recv(1, socket.MSG_PEEK))
                try:
                    super().close_request(request)
                finally:
                    closed.set()

        handler = partial(Handler, service=Service(), frontend=Path('frontend'))
        server = Server(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=3) as client:
                host = f'127.0.0.1:{server.server_address[1]}'
                declared = len(body) if length is None else length
                headers = (f'POST /api/tasks HTTP/1.1\r\nHost: {host}\r\n'
                           f'Content-Type: {content_type}\r\nContent-Length: {declared}\r\n')
                if origin:
                    headers += f'Origin: {origin}\r\n'
                client.sendall((headers + '\r\n').encode('ascii'))
                self.assertTrue(headers_parsed.wait(3))
                if body:
                    client.sendall(body)
                if half_close:
                    client.shutdown(socket.SHUT_WR)
                upload_sent.set()
                response = HTTPResponse(client)
                response.begin()
                facts.update(status=response.status, body=response.read(), connection=response.getheader('Connection'))
                self.assertTrue(closed.wait(3), 'Server exceeded the bounded shutdown wait')
                response.close()
        finally:
            upload_sent.set()
            server.shutdown()
            thread.join(3)
            server.server_close()
        return facts

    def test_rejections_send_json_and_drain_queued_body(self):
        for body, options, status, code in [
            (b'x' * 65537, {}, 413, 'REQUEST_TOO_LARGE'),
            (b'{"ok":true}', {'origin': 'https://outside.invalid'}, 403, 'ORIGIN_REJECTED'),
            (b'{}', {'content_type': 'text/plain'}, 415, 'UNSUPPORTED_MEDIA_TYPE'),
        ]:
            with self.subTest(code=code):
                facts = self.exercise(body, **options)
                self.assertEqual(status, facts['status'])
                self.assertIn(code.encode(), facts['body'])
                self.assertFalse(facts['unread_at_close'])
                self.assertEqual('close', facts['connection'])
                self.assertEqual([], facts['created'])

    def test_oversized_declaration_without_upload_still_finishes(self):
        facts = self.exercise(b'', length='9' * 100, half_close=False)
        self.assertEqual(413, facts['status'])
        self.assertIn(b'REQUEST_TOO_LARGE', facts['body'])
        self.assertEqual([], facts['created'])

    def test_normal_json_request_keeps_existing_behavior(self):
        facts = self.exercise(b'{"ok":true}')
        self.assertEqual(201, facts['status'])
        self.assertEqual([{'ok': True}], facts['created'])
        self.assertFalse(facts['unread_at_close'])
