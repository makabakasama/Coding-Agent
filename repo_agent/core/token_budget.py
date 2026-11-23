"""Model-specific token estimates for a Chat request before it reaches a Provider."""
from __future__ import annotations

import json
from pathlib import Path

from repo_agent.core.provider import ContextLimitError, ProviderError


class TokenBudget:
    def __init__(self, model: str, *, context_tokens: int, output_tokens: int,
                 reserve_tokens: int, tokenizer: str = 'auto'):
        if (not isinstance(model, str) or not model
                or any(type(value) is not int or value < 1
                       for value in (context_tokens, output_tokens, reserve_tokens))
                or context_tokens <= output_tokens + reserve_tokens):
            raise ValueError('Invalid Chat model token budget')
        self.model = model
        self.context_tokens = context_tokens
        self.output_tokens = output_tokens
        self.reserve_tokens = reserve_tokens
        self.input_tokens = context_tokens - output_tokens - reserve_tokens
        self.tokenizer = tokenizer
        self._encode, self.counter_name = self._load_counter(model, tokenizer)

    @staticmethod
    def _load_counter(model, specification):
        if specification == 'auto':
            if not model.startswith(('gpt-', 'o1', 'o3', 'o4')):
                raise ProviderError('Chat token 计数器无法自动匹配此模型；请配置 --chat-tokenizer')
            try:
                import tiktoken
                encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                # New compatible model aliases are not necessarily in tiktoken's
                # registry. This is an explicit estimate, recorded with the run.
                encoding = tiktoken.get_encoding('o200k_base')
            except ImportError as exc:
                raise ProviderError('Chat token 计数需要安装 tiktoken') from exc
            return lambda value: encoding.encode(value, disallowed_special=()), 'tiktoken:' + encoding.name
        if specification.startswith('tiktoken:'):
            try:
                import tiktoken
                encoding = tiktoken.get_encoding(specification.partition(':')[2])
            except ImportError as exc:
                raise ProviderError('Chat token 计数需要安装 tiktoken') from exc
            except ValueError as exc:
                raise ProviderError('Chat tiktoken 编码名称无效') from exc
            return lambda value: encoding.encode(value, disallowed_special=()), 'tiktoken:' + encoding.name
        if specification.startswith('hf:'):
            path = Path(specification.partition(':')[2]).expanduser()
            if not path.is_file():
                raise ProviderError('Chat tokenizer.json 文件不可用')
            try:
                from tokenizers import Tokenizer
                tokenizer = Tokenizer.from_file(str(path))
            except ImportError as exc:
                raise ProviderError('Chat token 计数需要安装 tokenizers') from exc
            except Exception as exc:
                raise ProviderError('Chat tokenizer.json 无法加载') from exc
            return lambda value: tokenizer.encode(value, add_special_tokens=False).ids, 'hf:' + path.name
        raise ProviderError('Chat tokenizer 配置须为 auto、tiktoken:<encoding> 或 hf:<tokenizer.json>')

    def count(self, messages: list[dict], tools: list[dict]) -> int:
        # Compatible APIs apply their own chat templates. Count actual field text
        # with the selected tokenizer and add explicit structural headroom.
        total = 16 + 12 * len(messages) + 32 * len(tools)
        for message in messages:
            for key, value in message.items():
                if isinstance(value, str):
                    total += len(self._encode(value))
                elif value is not None:
                    total += len(self._encode(json.dumps(value, ensure_ascii=False, sort_keys=True)))
        for tool in tools:
            total += len(self._encode(json.dumps(tool, ensure_ascii=False, sort_keys=True)))
        return total

    def require(self, messages: list[dict], tools: list[dict]) -> int:
        used = self.count(messages, tools)
        if used > self.input_tokens:
            raise ContextLimitError(f'Chat token budget exceeded: {used} > {self.input_tokens}')
        return used

    def manifest(self, used: int) -> dict:
        return {'model': self.model, 'counter': self.counter_name, 'method': 'estimate',
                'contextTokens': self.context_tokens, 'maxOutputTokens': self.output_tokens,
                'reservedTokens': self.reserve_tokens, 'maxInputTokens': self.input_tokens,
                'estimatedInputTokens': used}


class TurnBudgetExceeded(ProviderError):
    """A Chat turn has exhausted its cumulative resource allowance."""
