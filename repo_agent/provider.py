"""Tiny OpenAI-compatible chat completions adapter; avoids SDK lock-in."""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.request

from .config import read_dotenv
from .decisions import Decision, TOOL_DEFINITIONS, parse_tool_decision


class ProviderError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    def __init__(self, *, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: int = 90):
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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ProviderError(f"Model request failed: {type(exc).__name__}: {exc}") from exc
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Model response has no choices[0].message") from exc
        if choice.get("finish_reason") == "length":
            raise ProviderError("Model response was truncated; no tool was executed")
        return parse_tool_decision(message)
