"""Tiny OpenAI-compatible chat completions adapter; avoids SDK lock-in."""
from __future__ import annotations
import errno
import http.client
import json
import os
import ssl
import time
from typing import Callable
import urllib.error
import urllib.request

from .config import read_dotenv
from .decisions import Decision, TOOL_DEFINITIONS, parse_tool_decision


class ProviderError(RuntimeError):
    pass


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
        self._event_sink: Callable[[dict], None] | None = None
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
        previous, self._event_sink = self._event_sink, sink
        return previous

    def get_run_metadata(self) -> dict:
        """Only effective non-sensitive transport settings, for the start event."""
        return {"transport_retry": {"max_retries": self.max_retries,
                "max_attempts": self.max_retries + 1, "timeout_seconds": self.timeout,
                "retry_delays_seconds": list(RETRY_DELAYS[:self.max_retries]),
                "retry_http_errors": False}}

    def _emit_attempt(self, event: dict):
        if self._event_sink is not None:
            self._event_sink({"type": "model_request_attempt", **event})

    def _request_json(self, request: urllib.request.Request):
        # The Request (including serialized messages) is built once per decision.
        # Only transport is retried: no Agent step, protocol feedback or tool replay.
        maximum = self.max_retries + 1
        for attempt in range(1, maximum + 1):
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

    def decide(self, messages: list[dict]) -> Decision:
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload = json.dumps({"model": self.model, "messages": messages,
                              "temperature": 0, "tools": TOOL_DEFINITIONS,
                              "tool_choice": "auto", "parallel_tool_calls": False}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "coding-agent/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        data = self._request_json(request)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Model response has no choices[0].message") from exc
        if choice.get("finish_reason") == "length":
            raise ProviderError("Model response was truncated; no tool was executed")
        return parse_tool_decision(message)
