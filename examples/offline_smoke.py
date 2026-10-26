"""No API key required. Verify read → replace_lines → pytest → finish end-to-end."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from repo_agent.agent import Agent
from repo_agent.decisions import Decision
from repo_agent.tools import RepoTools


class ScriptedModel:
    def __init__(self):
        self.steps = iter([
            {"action": "read_file", "args": {"path": "calc.py", "start": 1, "end": 20}},
            {"action": "replace_lines", "args": {
                "start": 2, "end": 2, "new_text": "    return a + b\n"}},
            {"action": "run_tests", "args": {"target": "test_calc.py"}},
            {"action": "finish", "args": {"answer":
                "Changed subtraction to addition. Verified by test_calc.py."}},
        ])

    def decide(self, messages):
        decision = next(self.steps)
        if decision["action"] == "replace_lines":
            observation = json.loads(messages[-1]["content"])
            decision["args"]["snapshot_id"] = observation["data"]["snapshot_id"]
        return Decision(**decision)


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "sample_repo"
        root.mkdir()
        (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (root / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        tools = RepoTools(root, allow_writes=True, allow_tests=True)
        agent = Agent(ScriptedModel(), tools, trace_dir=Path(temp) / "traces", max_steps=5)
        result = agent.run("Fix the add() bug and verify with pytest")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        assert result["status"] == "agent_reported_finished"
        assert result["last_test_passed"] is True
        assert result["verified_local_change"] is True
        assert "return a + b" in (root / "calc.py").read_text()
        print("OFFLINE_SMOKE_PASS")


if __name__ == "__main__":
    main()
