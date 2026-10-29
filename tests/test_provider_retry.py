from copy import deepcopy
import http.client
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch
import urllib.error

from repo_agent.agent import Agent
from repo_agent.decisions import Decision, DecisionError
from repo_agent.provider import OpenAICompatibleProvider, ProviderError
from repo_agent.tools import RepoTools


def response(message=None, *, finish_reason="tool_calls"):
    message = message or Decision("finish", {"answer": "Inspected only."}, "call_finish").assistant_message
    return io.BytesIO(json.dumps({"choices": [{"message": message,
        "finish_reason": finish_reason}]}, ensure_ascii=False).encode("utf-8"))


def eof():
    return urllib.error.URLError(ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "fixture TLS EOF"))


class ProviderRetryTest(unittest.TestCase):
    def setUp(self):
        # All requests and waits are intercepted, including unexpected calls.
        for target, values in (("repo_agent.provider.read_dotenv", {"return_value": {}}),
                               ("repo_agent.provider.urllib.request.urlopen", {}),
                               ("repo_agent.provider.time.sleep", {})):
            patcher = patch(target, **values)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if target.endswith("urlopen"):
                self.send = mock
            elif target.endswith("sleep"):
                self.sleep = mock
        # Keep OS process-launch settings while removing all model/profile settings.
        launch_environment = {key: os.environ[key] for key in
            ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP") if key in os.environ}
        environment = patch.dict(os.environ, launch_environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.provider = OpenAICompatibleProvider(model="fixture", base_url="https://model.invalid/v1",
                                                  api_key="fixture-api-secret", timeout=7)
        self.events = []
        self.provider.set_event_sink(self.events.append)

    def test_tls_eof_then_success_preserves_serialized_full_messages(self):
        messages = [{"role": "system", "content": "完整上下文" * 5000},
                    {"role": "assistant", "content": None, "reasoning_content": "opaque metadata",
                     "tool_calls": [{"id": "prior", "type": "function", "function":
                        {"name": "list_files", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "prior", "content": "observation" * 3000}]
        original = deepcopy(messages)
        self.send.side_effect = [eof(), response()]
        self.assertEqual("finish", self.provider.decide(messages).action)
        first, second = [item.args[0] for item in self.send.call_args_list]
        self.assertEqual(first.data, second.data)
        self.assertEqual(original, json.loads(second.data)["messages"])
        self.assertEqual(original, messages)
        self.assertEqual([7, 7], [item.kwargs["timeout"] for item in self.send.call_args_list])
        self.sleep.assert_called_once_with(0.5)
        self.assertEqual(["failed", "response_received"], [e["outcome"] for e in self.events])
        self.assertEqual([1, 2], [e["attempt"] for e in self.events])
        self.assertEqual("SSLEOFError", self.events[0]["cause_type"])
        self.assertEqual("tls_eof", self.events[0]["reason"])
        self.assertTrue(self.events[0]["will_retry"])

    def test_timeouts_and_interrupted_connections_are_retried(self):
        errors = [TimeoutError("timeout"), urllib.error.URLError(TimeoutError("wrapped timeout")),
                  ConnectionResetError("reset"), ConnectionAbortedError("aborted"), BrokenPipeError("pipe"),
                  http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b"partial", 10),
                  ssl.SSLError(ssl.SSL_ERROR_EOF, "generic SSL EOF"),
                  urllib.error.URLError(urllib.error.URLError(ssl.SSLEOFError(8, "nested EOF")))]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.send.reset_mock()
                self.sleep.reset_mock()
                self.events.clear()
                self.send.side_effect = [error, response()]
                self.provider.decide([])
                self.assertEqual(2, self.send.call_count)
                self.sleep.assert_called_once_with(0.5)
                self.assertTrue(self.events[0]["retryable"])

    def test_response_read_failure_retries_transport_not_only_urlopen(self):
        class BrokenResponse(io.BytesIO):
            def read(self, *args, **kwargs):
                raise ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "EOF while reading response")
        broken = BrokenResponse()
        self.send.side_effect = [broken, response()]
        self.provider.decide([])
        self.assertTrue(broken.closed)
        self.assertEqual(2, self.send.call_count)

    def test_retries_exhaust_with_explicit_cause_and_last_failure_event(self):
        self.send.side_effect = [eof(), eof(), eof()]
        with self.assertRaises(ProviderError) as error:
            self.provider.decide([])
        self.assertIn("3 attempt(s)", str(error.exception))
        self.assertIn("SSLEOFError (tls_eof)", str(error.exception))
        self.assertIsInstance(error.exception.__cause__, urllib.error.URLError)
        self.assertEqual(3, self.send.call_count)
        self.assertEqual([call(0.5), call(1.0)], self.sleep.call_args_list)
        self.assertEqual([1, 2, 3], [e["attempt"] for e in self.events])
        self.assertEqual([True, True, False], [e["will_retry"] for e in self.events])
        self.assertEqual(0.0, self.events[-1]["retry_delay_seconds"])
        self.assertTrue(self.events[-1]["retryable"])

    def test_certificate_http_and_unclassified_errors_are_not_retried_or_leaked(self):
        secret_url = "https://user:url-secret@model.invalid/v1?api_key=query-secret"
        errors = [ssl.SSLCertVerificationError(1, "certificate-secret"),
                  urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate-secret")),
                  ssl.SSLError(1, "permanent TLS configuration error"),
                  urllib.error.URLError("SSL: UNEXPECTED_EOF_WHILE_READING string-secret"),
                  urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "unresolved host"))]
        errors += [urllib.error.HTTPError(secret_url, code, "body-secret",
                    {"Authorization": "Bearer header-secret"}, None) for code in (400, 401, 403, 429, 500)]
        for error in errors:
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)):
                self.send.reset_mock()
                self.sleep.reset_mock()
                self.events.clear()
                self.send.side_effect = error
                with self.assertRaises(ProviderError) as raised:
                    self.provider.decide([])
                self.assertEqual(1, self.send.call_count)
                self.sleep.assert_not_called()
                self.assertFalse(self.events[-1]["retryable"])
                self.assertFalse(self.events[-1]["will_retry"])
                saved = json.dumps(self.events) + str(raised.exception)
                for secret in (secret_url, "fixture-api-secret", "url-secret", "query-secret",
                               "body-secret", "header-secret", "certificate-secret", "string-secret", "Authorization"):
                    self.assertNotIn(secret, saved)

    def test_invalid_json_structure_arguments_and_truncation_do_not_retry(self):
        bad = Decision("read_file", {"path": "sample.py"}, "call_bad").assistant_message
        bad["tool_calls"][0]["function"]["arguments"] = '{"path":'
        cases = [(io.BytesIO(b"not JSON"), ProviderError),
                 (io.BytesIO(b'{}'), ProviderError), (response(bad), DecisionError),
                 (response(finish_reason="length"), ProviderError)]
        for body, expected_error in cases:
            with self.subTest(error=expected_error.__name__):
                self.send.reset_mock()
                self.sleep.reset_mock()
                self.send.side_effect = None
                self.send.return_value = body
                with self.assertRaises(expected_error):
                    self.provider.decide([])
                self.assertEqual(1, self.send.call_count)
                self.sleep.assert_not_called()

    def test_retry_budget_resets_for_each_decision_and_can_be_disabled(self):
        self.send.side_effect = [eof(), eof(), response(), eof(), eof(), response()]
        self.provider.decide([])
        self.provider.decide([])
        self.assertEqual([1, 2, 3, 1, 2, 3], [e["attempt"] for e in self.events])
        self.assertEqual([call(0.5), call(1.0)] * 2, self.sleep.call_args_list)
        single = OpenAICompatibleProvider(model="fixture", base_url="https://model.invalid/v1",
                                           api_key="", max_retries=0)
        self.send.reset_mock()
        self.sleep.reset_mock()
        self.send.side_effect = eof()
        with self.assertRaisesRegex(ProviderError, "1 attempt"):
            single.decide([])
        self.assertEqual(1, self.send.call_count)
        self.sleep.assert_not_called()
        for invalid in (-1, 3, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                OpenAICompatibleProvider(max_retries=invalid)

    def test_agent_logs_policy_and_retry_before_wait_without_replaying_successful_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_bytes(b"print('old')\n")
            subprocess.run(["git", "-c", "safe.directory=" + root.as_posix(), "init", "-q", str(root)],
                           check=True, capture_output=True)
            tools = RepoTools(root, allow_writes=True)
            agent = Agent(self.provider, tools, trace_dir=root / "trace", max_steps=3)
            requests = []

            def send(request, **kwargs):
                requests.append(request.data)
                if len(requests) == 1:
                    return response(Decision("read_file", {"path": "sample.py"}, "read").assistant_message)
                if len(requests) == 2:
                    observation = json.loads(json.loads(request.data)["messages"][-1]["content"])
                    return response(Decision("replace_lines", {"snapshot_id": observation["data"]["snapshot_id"],
                        "start": 1, "end": 1, "new_text": "print('new')\n"}, "edit").assistant_message)
                if len(requests) == 3:
                    raise eof()
                return response()

            def check_before_wait(delay):
                event = json.loads(agent.trace_path.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(("model_request_attempt", 3, 1, True),
                    (event["type"], event["step"], event["attempt"], event["will_retry"]))

            self.send.side_effect = send
            self.sleep.side_effect = check_before_wait
            with patch.object(tools, "execute", wraps=tools.execute) as execute:
                result = agent.run("Edit one line; report tests unverified")
                self.assertEqual(["read_file", "replace_lines"], [c.args[0] for c in execute.call_args_list])
            self.assertEqual("agent_reported_finished", result["status"])
            self.assertEqual(3, result["steps"])
            self.assertEqual(2, result["tool_actions"])
            self.assertEqual(b"print('new')\n", (root / "sample.py").read_bytes())
            self.assertEqual(requests[2], requests[3])
            events = [json.loads(line) for line in agent.trace_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(3, events[0]["max_steps"])
            policy = events[0]["model_runtime"]["transport_retry"]
            self.assertEqual({"max_retries": 2, "max_attempts": 3, "timeout_seconds": 7,
                "retry_delays_seconds": [0.5, 1.0], "retry_http_errors": False}, policy)
            retry_events = [e for e in events if e["type"] == "model_request_attempt" and e["step"] == 3]
            self.assertEqual([1, 2], [e["attempt"] for e in retry_events])
            self.assertIsNone(result["last_test_passed"])
            self.assertEqual(self.events.append, self.provider.set_event_sink(None))
            self.assertNotIn("fixture-api-secret", agent.trace_path.read_text(encoding="utf-8"))

    def test_agent_final_error_keeps_all_exhausted_attempts_and_prior_tool_output(self):
        self.send.side_effect = [response(Decision("list_files", {}, "listed").assistant_message), eof(), eof(), eof()]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = RepoTools(root)
            agent = Agent(self.provider, tools, trace_dir=root / "trace", max_steps=2)
            with patch.object(tools, "execute", wraps=tools.execute) as execute:
                result = agent.run("Inspect only")
                execute.assert_called_once_with("list_files", {})
            events = [json.loads(line) for line in agent.trace_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual("error", result["status"])
            self.assertEqual(2, result["steps"])
            self.assertIn("3 attempt(s)", result["reason"])
            self.assertEqual(4, self.send.call_count)
            failed = [e for e in events if e.get("outcome") == "failed"]
            self.assertEqual([2, 2, 2], [e["step"] for e in failed])
            self.assertEqual([1, 2, 3], [e["attempt"] for e in failed])
            self.assertEqual("error", events[-1]["type"])
            self.assertFalse(failed[-1]["will_retry"])
            self.assertTrue(any(e["type"] == "observation" for e in events))
            self.assertEqual(self.events.append, self.provider.set_event_sink(None))

    def test_protocol_feedback_after_transport_recovery_still_uses_next_decision_step(self):
        bad = Decision("read_file", {"path": "sample.py"}, "bad").assistant_message
        bad["tool_calls"][0]["function"]["arguments"] = '{"path":'
        self.send.side_effect = [eof(), response(bad), response()]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = RepoTools(root)
            with patch.object(tools, "execute", wraps=tools.execute) as execute:
                agent = Agent(self.provider, tools, trace_dir=root / "trace", max_steps=2)
                result = agent.run("Inspect only")
                execute.assert_not_called()
            self.assertEqual("agent_reported_finished", result["status"])
            self.assertEqual(2, result["steps"])
            history = json.loads(self.send.call_args_list[-1].args[0].data)["messages"]
            self.assertEqual("bad", history[-1]["tool_call_id"])
            self.assertFalse(json.loads(history[-1]["content"])["ok"])
            events = [json.loads(line) for line in agent.trace_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(1, next(e for e in events if e["type"] == "decision_error")["step"])


if __name__ == "__main__":
    unittest.main()
