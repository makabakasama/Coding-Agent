"""Explicit injected-model HTTP fixture, always creates its own synthetic repo.

No repository argument or provider is accepted. Materials are retained in the
printed temporary directory, not automatically removed on exit.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from repo_agent.decisions import Decision
from repo_agent.task_http import create_server
from repo_agent.task_projects import TaskProjects
from repo_agent.task_service import TaskService
from repo_agent.task_store import TaskStore


class FixtureModel:
    def __init__(self):
        self.step = 0

    def decide(self, messages):
        self.step += 1
        if self.step == 1:
            return Decision("read_file", {"path": "calc.py"})
        if self.step == 2:
            observation = json.loads(messages[-1]["content"])
            return Decision("replace_lines", {"snapshot_id": observation["data"]["snapshot_id"],
                                               "start": 2, "end": 2, "new_text": "    return a + b\n"})
        return Decision("finish", {"answer": "离线脚本已将减法改为加法；最终检查由后端独立运行。"})


def build_fixture():
    root = Path(tempfile.mkdtemp(prefix="coding-agent-m3-fixture-" )).resolve()
    repo = root / "synthetic-repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=20)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "synthetic addition bug")
    projects = root / "projects.json"
    projects.write_text(json.dumps({"projects": [{"projectId": "offline-fixture", "name": "离线脚本 · 临时合成仓库",
                       "repo": "synthetic-repo", "execution": {"testPython": sys.executable}}]}), encoding="utf-8")
    service = TaskService(TaskStore(root / "tasks"), TaskProjects(projects), workspace_root=root / "workspaces", model_factory=FixtureModel)
    task = service.create({"projectId": "offline-fixture", "revision": "HEAD", "current": "add(2,3) 返回 -1",
                           "expected": "返回 5", "allowedPaths": ["calc.py"], "allowTests": True,
                           "checkTargets": ["tests/test_calc.py"], "maxSteps": 5})
    return root, service, task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="0 selects an unused local port")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0..65535")
    root, service, task = build_fixture()
    server = create_server(service, port=args.port)
    print(json.dumps({"source": "injected_test_model", "fixtureRoot": str(root), "taskId": task["taskId"],
                      "url": f"http://127.0.0.1:{server.server_port}/#/tasks/{task['taskId']}"}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
