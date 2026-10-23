from __future__ import annotations
import argparse
import json
from pathlib import Path

from .agent import Agent
from .provider import OpenAICompatibleProvider, ProviderError
from .tools import RepoTools


def main():
    p = argparse.ArgumentParser(description="Minimal repo Coding Agent — only use disposable trusted checkouts")
    p.add_argument("--repo", type=Path, required=True, help="Local git repo path")
    p.add_argument("--task", required=True, help="Issue / coding task description")
    p.add_argument("--model", help="Override OPENAI_MODEL")
    p.add_argument("--base-url", help="Override OPENAI_BASE_URL")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--trace-dir", type=Path, default=Path("runs"))
    p.add_argument("--allow-writes", action="store_true", help="Allow git apply on checkout")
    p.add_argument("--allow-tests", action="store_true", help="Allow pytest; may execute untrusted repo code")
    p.add_argument("--test-python", type=Path, help="Python executable for the target repository's test environment")
    args = p.parse_args()
    try:
        tools = RepoTools(args.repo, allow_writes=args.allow_writes,
                          allow_tests=args.allow_tests, test_python=args.test_python)
        model = OpenAICompatibleProvider(model=args.model, base_url=args.base_url)
        result = Agent(model, tools, trace_dir=args.trace_dir, max_steps=args.max_steps).run(args.task)
    except (OSError, ValueError, ProviderError) as exc:
        p.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
