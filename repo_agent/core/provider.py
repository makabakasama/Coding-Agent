"""Tiny OpenAI-compatible chat completions adapter; avoids SDK lock-in."""
from __future__ import annotations
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import errno
import http.client
import hashlib
import json
import os
import ssl
import time
from typing import Callable
import urllib.error
import urllib.request

from repo_agent.core.config import read_dotenv
from repo_agent.core.decisions import Decision, TOOL_DEFINITIONS, parse_tool_decision


class ProviderError(RuntimeError):
    pass


class ContextLimitError(ProviderError):
    pass


@dataclass
class TextReply:
    text: str
    assistant_message: dict


@dataclass
class ToolCallReply:
    decision: Decision


RETRY_DELAYS = (0.5, 1.0)


def _request_failure(exc: Exception) -> dict:
    """Classify transport failures structurally; never retain raw URLs/headers."""
    cause = exc
    seen = set()
    while (isinstance(cause, urllib.error.URLError) and not isinstance(cause, urllib.error.HTTPError)
           and isinstance(cause.reason, BaseException) and id(cause) not in seen):
        seen.add(id(cause))
        cause = cause.reason
    retryable = False
    if isinstance(cause, urllib.error.HTTPError):
        reason = "http_error"
    elif isinstance(cause, ssl.SSLCertVerificationError):
        reason = "certificate_verification_failed"
    elif isinstance(cause, ssl.SSLError):
        retryable = (isinstance(cause, ssl.SSLEOFError)
                     or cause.errno == ssl.SSL_ERROR_EOF
                     or getattr(cause, "reason", None) == "UNEXPECTED_EOF_WHILE_READING")
        reason = "tls_eof" if retryable else "tls_error"
    elif isinstance(cause, TimeoutError):
        retryable, reason = True, "timeout"
    elif (isinstance(cause, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                             http.client.RemoteDisconnected, http.client.IncompleteRead))
          or isinstance(cause, OSError) and cause.errno in
          (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ETIMEDOUT)):
        retryable, reason = True, "connection_interrupted"
    elif isinstance(cause, ValueError):
        reason = "invalid_response_json"
    else:
        reason = "request_error"
    result = {"error_type": type(exc).__name__, "cause_type": type(cause).__name__,
              "reason": reason, "retryable": retryable}
    if isinstance(cause, urllib.error.HTTPError):
        result["http_status"] = cause.code
    return result


class OpenAICompatibleProvider:
    def __init__(self, *, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: int = 90, max_retries: int = 2):
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("max_retries must be an integer in 0..2")
        self.max_retries = max_retries
        self._tool_definitions = deepcopy(TOOL_DEFINITIONS)
        self._events: ContextVar[Callable[[dict], None] | None] = ContextVar('provider_events', default=None)
        local = read_dotenv()
        def setting(*keys: str) -> str:
            return next((source[key] for source in (os.environ, local)
                         for key in keys if source.get(key)), "")

        if setting("LLM_PROVIDER") not in ("", "openai_compatible"):
            raise ProviderError("LLM_PROVIDER must be openai_compatible")
        self.profile = setting("LLM_PROFILE")
        if self.profile and self.profile not in ("qwen", "gpt"):
            raise ProviderError("LLM_PROFILE must be qwen or gpt")
        # A selected profile never falls back to another profile or legacy keys.
        prefix = self.profile.upper()
        model_keys = (f"{prefix}_MODEL",) if prefix else ("LLM_MODEL", "OPENAI_MODEL")
        url_keys = (f"{prefix}_BASE_URL",) if prefix else ("LLM_BASE_URL", "OPENAI_BASE_URL")
        key_keys = (f"{prefix}_API_KEY",) if prefix else ("LLM_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY")
        self.model = model or setting(*model_keys)
        self.base_url = (base_url or setting(*url_keys)).rstrip("/")
        self.api_key = (api_key if api_key is not None else
                        setting(*key_keys))
        self.timeout = timeout
        if not self.model or not self.base_url:
            raise ProviderError("Set the selected profile's MODEL and BASE_URL in .env or environment (or LLM_MODEL and LLM_BASE_URL without LLM_PROFILE).")

    def set_event_sink(self, sink: Callable[[dict], None] | None) -> Callable[[dict], None] | None:
        """Optional model telemetry hook; Agent restores the previous sink per step."""
        previous = self._event_sink
        self._events.set(sink)
        return previous

    @property
    def _event_sink(self):
        return self._events.get()

    def get_run_metadata(self) -> dict:
        """Only effective non-sensitive transport settings, for the start event."""
        return {"transport_retry": {"max_retries": self.max_retries,
                "max_attempts": self.max_retries + 1, "timeout_seconds": self.timeout,
                "retry_delays_seconds": list(RETRY_DELAYS[:self.max_retries]),
                "retry_http_errors": False},
                "token_budget_capabilities": {
                    "input_count": "local_estimate",
                    "output_limit_parameter": "max_tokens",
                    "provider_enforcement_verified": False,
                    "aggregate_token_limit": False,
                    "bounded_decision_attempt_starts": True}}

    def get_tool_definitions(self) -> list[dict]:
        return deepcopy(self._tool_definitions)

    def configure_development_checks(self, *, temporary: bool):
        from repo_agent.core.check_tools import configure_check_tools

        configure_check_tools(self._tool_definitions, temporary=temporary)

    def _emit_attempt(self, event: dict):
        if self._event_sink is not None:
            self._event_sink({"type": "model_request_attempt", **event})

    def _request_json(self, request: urllib.request.Request, *, record_starts=False):
        # The Request (including serialized messages) is built once per decision.
        # Only transport is retried: no Agent step, protocol feedback or tool replay.
        maximum = self.max_retries + 1
        for attempt in range(1, maximum + 1):
            if record_starts:
                self._emit_attempt({'attempt': attempt, 'max_attempts': maximum, 'outcome': 'started'})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.load(response)
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
                failure = _request_failure(exc)
                retry = failure["retryable"] and attempt < maximum
                delay = RETRY_DELAYS[attempt - 1] if retry else 0.0
                self._emit_attempt({"attempt": attempt, "max_attempts": maximum, "outcome": "failed",
                                    **failure, "will_retry": retry, "retry_delay_seconds": delay})
                if not retry:
                    # Do not stringify exceptions: a URL or upstream reason can contain secrets.
                    detail = f"{failure['error_type']} / {failure['cause_type']} ({failure['reason']})"
                    if "http_status" in failure:
                        detail += f" HTTP {failure['http_status']}"
                    raise ProviderError(f"Model request failed after {attempt} attempt(s): {detail}") from exc
                time.sleep(delay)
            else:
                self._emit_attempt({"attempt": attempt, "max_attempts": maximum,
                                    "outcome": "response_received", "will_retry": False})
                return data

    def _request_stream(self, request):
        """Consume SSE incrementally; never replay a partially received response."""
        maximum = self.max_retries + 1
        for attempt in range(1, maximum + 1):
            received = False
            self._emit_attempt({'attempt': attempt, 'max_attempts': maximum, 'outcome': 'started'})
            message = {'role': 'assistant', 'content': ''}
            calls, usage, finish = {}, None, None
            published = 0.0
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    fields, done = [], False
                    for raw in response:
                        line = raw.decode('utf-8').rstrip('\r\n')
                        if line.startswith('data:'):
                            fields.append(line[5:].lstrip(' '))
                        elif not line and fields:
                            payload = '\n'.join(fields)
                            fields = []
                            if payload == '[DONE]':
                                done = True
                                break
                            part = json.loads(payload)
                            received = True
                            if not isinstance(part, dict):
                                raise ProviderError('Model stream returned an invalid event')
                            if part.get('error'):
                                raise ProviderError('Model stream returned an upstream error')
                            if part.get('usage') is not None:
                                usage = part['usage']
                            for choice in part.get('choices', []):
                                if choice.get('index', 0) != 0:
                                    continue
                                delta = choice.get('delta') or {}
                                text = delta.get('content')
                                if isinstance(text, str):
                                    message['content'] += text
                                    if self._event_sink is not None and time.monotonic() - published >= 0.08:
                                        self._event_sink({'type': 'model_text', 'text': message['content']})
                                        published = time.monotonic()
                                for call in delta.get('tool_calls') or []:
                                    index = call['index']
                                    target = calls.setdefault(index, {'id': '', 'type': 'function',
                                                                     'function': {'name': '', 'arguments': ''}})
                                    target['id'] += call.get('id') or ''
                                    for key in ('name', 'arguments'):
                                        target['function'][key] += (call.get('function') or {}).get(key) or ''
                                if choice.get('finish_reason') is not None:
                                    finish = choice['finish_reason']
                    if not done or finish is None:
                        raise ProviderError('Model stream ended before completion; partial text is not a saved answer')
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError, ProviderError) as exc:
                failure = _request_failure(exc)
                retry = not received and failure['retryable'] and attempt < maximum
                delay = RETRY_DELAYS[attempt - 1] if retry else 0.0
                self._emit_attempt({'attempt': attempt, 'max_attempts': maximum, 'outcome': 'failed',
                                    **failure, 'will_retry': retry, 'retry_delay_seconds': delay})
                if not retry:
                    raise ProviderError('Model streaming request failed; partial output was not completed') from exc
                time.sleep(delay)
                continue
            if calls:
                message['tool_calls'] = [calls[index] for index in sorted(calls)]
            if self._event_sink is not None and message['content']:
                self._event_sink({'type': 'model_text', 'text': message['content'], 'complete': True})
            self._emit_attempt({'attempt': attempt, 'max_attempts': maximum,
                                'outcome': 'response_received', 'will_retry': False})
            return {'choices': [{'message': message, 'finish_reason': finish}], 'usage': usage}

    def _completion(self, messages, tools, response_limits=None, *, record_input=False, stream=False,
                    record_attempt_starts=False):
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        body = {"model": self.model, "messages": messages, "temperature": 0}
        if stream:
            body.update(stream=True, stream_options={'include_usage': True})
        if tools:
            body.update(tools=tools, tool_choice='auto', parallel_tool_calls=False)
        limits = {} if response_limits is None else response_limits
        if (type(limits) is not dict or set(limits) - {'max_tokens', 'max_input_bytes'}
                or any(type(value) is not int or value < 1 for value in limits.values())):
            raise ValueError('Response limits must contain positive integer max_tokens/max_input_bytes')
        if 'max_tokens' in limits:
            body['max_tokens'] = limits['max_tokens']
        payload = json.dumps(body).encode('utf-8')
        if record_input and self._event_sink is not None:
            self._event_sink({'type': 'model_input', 'request': deepcopy(body),
                              'requestSha256': hashlib.sha256(payload).hexdigest(),
                              'requestBytes': len(payload)})
        if 'max_input_bytes' in limits and len(payload) > limits['max_input_bytes']:
            if self._event_sink is not None and record_attempt_starts:
                self._event_sink({'type': 'model_request_not_sent', 'reason': 'payload_size_limit'})
            raise ContextLimitError('CONTEXT_LIMIT: serialized model request exceeds the input limit')
        headers = {"Content-Type": "application/json", "User-Agent": "coding-agent/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        data = (self._request_stream(request) if stream else
                self._request_json(request, record_starts=record_input or record_attempt_starts))
        if record_input and self._event_sink is not None:
            self._event_sink({'type': 'model_response', 'response': deepcopy(data)})
        if self._event_sink is not None:
            usage = data.get('usage') if isinstance(data, dict) else None
            counts = {}
            if isinstance(usage, dict):
                for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens'):
                    if type(usage.get(key)) is int and usage[key] >= 0:
                        counts[key] = usage[key]
                for detail_key, alias, field in (
                        ('prompt_tokens_details', 'input_tokens_details', 'cached_tokens'),
                        ('completion_tokens_details', 'output_tokens_details', 'reasoning_tokens')):
                    for details in (usage.get(detail_key), usage.get(alias), usage):
                        value = details.get(field) if isinstance(details, dict) else None
                        if type(value) is int and value >= 0:
                            counts[field] = value
                            break
            self._event_sink({'type': 'model_usage', 'available': bool(counts), 'usage': counts})
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Model response has no choices[0].message") from exc
        if choice.get("finish_reason") == "length":
            raise ProviderError("Model response was truncated; no tool was executed")
        if choice.get('finish_reason') == 'content_filter':
            raise ProviderError('Model response was filtered')
        return message

    def decide(self, messages: list[dict]) -> Decision:
        return parse_tool_decision(self._completion(messages, self._tool_definitions))

    def decide_with_limits(self, messages: list[dict], response_limits: dict) -> Decision:
        return parse_tool_decision(self._completion(messages, self._tool_definitions,
                                                    response_limits, record_attempt_starts=True))

    def decide_with_tools(self, messages: list[dict], response_limits: dict,
                          tool_names: list[str]) -> Decision:
        if not isinstance(tool_names, list) or any(not isinstance(name, str) for name in tool_names):
            raise ValueError('tool_names must be a list of tool names')
        available = {item['function']['name']: item for item in self._tool_definitions}
        if set(tool_names) - available.keys():
            raise ValueError('Requested tool is not available')
        tools = [available[name] for name in tool_names]
        return parse_tool_decision(self._completion(messages, tools, response_limits, record_attempt_starts=True))

    def respond(self, messages: list[dict], tools=None, response_limits=None) -> TextReply | ToolCallReply:
        return self._respond(messages, tools, response_limits)

    def respond_stream(self, messages: list[dict], tools=None, response_limits=None) -> TextReply | ToolCallReply:
        return self._respond(messages, tools, response_limits, stream=True)

    def _respond(self, messages, tools=None, response_limits=None, *, stream=False):
        """Return text or a validated native decision using only explicitly offered tools."""
        offered = [] if tools is None else deepcopy(tools)
        if not isinstance(offered, list):
            raise ValueError('Tools must be a list')
        try:
            names = [tool['function']['name'] for tool in offered]
            if (any(tool.get('type') != 'function' for tool in offered)
                    or any(not isinstance(name, str) or not name for name in names)
                    or len(set(names)) != len(names)):
                raise ValueError('Invalid tool definitions')
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Invalid tool definitions') from exc
        limits = {'max_input_bytes': 48000}
        if response_limits is not None:
            if type(response_limits) is not dict:
                raise ValueError('Response limits must be an object')
            limits.update(response_limits)
        message = self._completion(messages, offered, limits, record_input=True, stream=stream)
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            raise ProviderError('Model response is not an assistant message')
        calls = message.get('tool_calls')
        if calls:
            decision = parse_tool_decision(message)
            if decision.action not in names:
                raise ProviderError('Model called a tool that was not offered')
            return ToolCallReply(decision)
        if calls is not None and calls != []:
            raise ProviderError('Model returned invalid tool calls')
        text = message.get('content')
        if not isinstance(text, str) or not text.strip() or '\x00' in text:
            raise ProviderError('Model response has no nonempty text')
        try:
            text.encode('utf-8')
        except UnicodeError as exc:
            raise ProviderError('Model response contains invalid text') from exc
        return TextReply(text, deepcopy(message))
