"""Verify a real model calls a read-only tool and consumes its observation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import tempfile

from repo_agent.agent import Agent
from repo_agent.provider import OpenAICompatibleProvider, ProviderError
from repo_agent.tools import RepoTools


def run_smoke(model, trace_dir: Path) -> dict:
    # Send only synthetic data to the provider, not the user's project files.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        marker = "probe_" + secrets.token_hex(8) + ".txt"
        (root / marker).write_text("Synthetic model connectivity probe.\n", encoding="utf-8")
        agent = Agent(model, RepoTools(root), trace_dir=trace_dir, max_steps=3)
        result = agent.run(
            "Connectivity check only. First call list_files with subdir '.'. "
            "Then finish with the exact probe_*.txt filename returned in the observation. "
            "Do not use any other tool."
        )
        events = [json.loads(line) for line in agent.trace_path.read_text(encoding="utf-8").splitlines()]
        decisions = [event["action"] for event in events if event["type"] == "decision"]
        observed = any(event["type"] == "observation" and event["action"] == "list_files"
                       and event["result"]["ok"] for event in events)
        passed = (result["status"] == "agent_reported_finished"
                  and decisions == ["list_files", "finish"] and observed
                  and marker in result.get("answer", ""))
        return {"smoke_passed": passed, "actions": decisions, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override OPENAI_MODEL (Qwen or another compatible model)")
    parser.add_argument("--base-url", help="Override OPENAI_BASE_URL")
    parser.add_argument("--trace-dir", type=Path, default=Path("runs/live-smoke"))
    args = parser.parse_args()
    try:
        model = OpenAICompatibleProvider(model=args.model, base_url=args.base_url)
        result = run_smoke(model, args.trace_dir)
    except (OSError, ValueError, ProviderError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("LIVE_LLM_SMOKE_PASS" if result["smoke_passed"] else "LIVE_LLM_SMOKE_FAIL")
    raise SystemExit(0 if result["smoke_passed"] else 1)


if __name__ == "__main__":
    main()
