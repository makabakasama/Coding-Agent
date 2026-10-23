"""Tiny OpenAI-compatible chat completions adapter; avoids SDK lock-in."""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.request

from .config import read_dotenv


class ProviderError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    def __init__(self, *, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: int = 90):
        local = read_dotenv()
        self.model = model or os.getenv("OPENAI_MODEL") or local.get("OPENAI_MODEL", "")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                         or local.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = (api_key if api_key is not None else
                        os.getenv("OPENAI_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
                        or local.get("OPENAI_API_KEY") or local.get("DASHSCOPE_API_KEY", ""))
        self.timeout = timeout
        if not self.model or not self.base_url:
            raise ProviderError("Set OPENAI_MODEL and OPENAI_BASE_URL in the project .env or environment (and an API key if required).")

    def decide(self, messages: list[dict[str, str]]) -> str:
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload = json.dumps({"model": self.model, "messages": messages,
                              "temperature": 0}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ProviderError(f"Model request failed: {type(exc).__name__}: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Model response has no choices[0].message.content") from exc
        if not isinstance(content, str):
            raise ProviderError("Model must return text JSON in message.content")
        return content
