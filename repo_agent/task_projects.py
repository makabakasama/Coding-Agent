"""Local project registry and read-only Git revision resolution."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from .task_contract import COMMIT, PROJECT_ID, TaskError, load_json


class TaskProjects:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()

    def _projects(self) -> dict:
        try:
            with self.config_path.open("rb") as stream:
                data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Oversized config")
            value = load_json(data)
            if type(value) is not dict or set(value) != {"projects"} or type(value["projects"]) is not list:
                raise ValueError("Invalid registry")
            result = {}
            for project in value["projects"]:
                if (type(project) is not dict or not {"projectId", "name", "repo"} <= set(project)
                        or set(project) - {"projectId", "name", "repo", "execution"}
                        or any(type(project[k]) is not str or not project[k].strip() for k in ("projectId", "name", "repo"))
                        or not PROJECT_ID.fullmatch(project["projectId"])
                        or project["projectId"] in result):
                    raise ValueError("Invalid project")
                result[project["projectId"]] = project
            return result
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise TaskError(503, "PROJECT_CONFIG_UNAVAILABLE", "本机项目配置无法读取") from exc

    def list(self) -> list[dict]:
        return [{"projectId": p["projectId"], "name": p["name"]}
                for p in self._projects().values()]

    def resolve(self, project_id: str, revision: str) -> str:
        project = self._projects().get(project_id)
        if project is None:
            raise TaskError(400, "UNKNOWN_PROJECT", "未配置该项目")
        try:
            repo = Path(project["repo"])
            if not repo.is_absolute():
                repo = self.config_path.parent / repo
            repo = repo.resolve(strict=True)

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "core.longpaths=true", "-C", str(repo), *args],
                    capture_output=True, text=True, encoding="utf-8", timeout=15)

            probe = git("rev-parse", "--is-inside-work-tree")
            if probe.returncode or probe.stdout.strip() != "true":
                raise OSError("Unavailable repository")
            top = git("rev-parse", "--show-toplevel")
            if top.returncode or Path(top.stdout.strip()).resolve() != repo:
                raise OSError("Project must name a Git repository root")
            resolved = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
            if resolved.returncode:
                raise TaskError(400, "INVALID_REVISION", "起始版本无法解析为 commit")
            commit = resolved.stdout.strip()
            if not COMMIT.fullmatch(commit):
                raise OSError("Invalid Git output")
            return commit
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise TaskError(503, "PROJECT_UNAVAILABLE", "本机仓库或 Git 暂不可用") from exc

    def repository(self, project_id: str, commit: str) -> Path:
        # Validate the configured root and the frozen commit, never the old branch.
        self.resolve(project_id, commit)
        project = self._projects().get(project_id)
        if project is None:
            raise TaskError(400, "UNKNOWN_PROJECT", "未配置该项目")
        repo = Path(project["repo"])
        return (repo if repo.is_absolute() else self.config_path.parent / repo).resolve(strict=True)

    def execution_config(self, project_id):
        try:
            project = self._projects()[project_id]
            config = project.get("execution", {})
            if type(config) is not dict or set(config) - {"testPython", "testEnv", "testTimeout"}:
                raise ValueError("Invalid execution config")
            python = config.get("testPython", sys.executable)
            if type(python) is not str or not python.strip():
                raise ValueError("Invalid interpreter")
            python = Path(python)
            python = (python if python.is_absolute() else self.config_path.parent / python).resolve(strict=True)
            if not python.is_file():
                raise ValueError("Missing interpreter")
            timeout = config.get("testTimeout", 90)
            env = config.get("testEnv", {})
            if type(timeout) is not int or not 1 <= timeout <= 300 or type(env) is not dict:
                raise ValueError("Invalid timeout/environment")
            if set(env) - {"PYTHONHASHSEED", "PYTHONUTF8", "PYTHONIOENCODING", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}:
                raise ValueError("Environment key not allowed")
            if any(type(v) is not str or len(v) > 200 or any(ord(c) < 32 for c in v) for v in env.values()):
                raise ValueError("Invalid environment value")
            return {"test_python": python, "test_env": env, "test_timeout": timeout}
        except (KeyError, OSError, ValueError, TaskError) as exc:
            raise TaskError(409, "EXECUTION_CONFIG_UNAVAILABLE", "本机执行配置不可用") from exc
