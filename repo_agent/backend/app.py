"""Compose repositories, services, HTTP transport and configuration."""
import argparse
import os
import sys
from functools import partial
from pathlib import Path
from repo_agent.backend.routers.task_http import TaskHandler, TaskHTTPServer
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.repositories.task_projects import TaskProjects


def create_server(service: TaskService, *, port: int = 4173, frontend: Path | None = None):
    frontend = frontend or Path(__file__).resolve().parents[2] / "frontend"
    return TaskHTTPServer(("127.0.0.1", port), partial(TaskHandler, service=service, frontend=frontend))


def add_storage_arguments(parser):
    parser.add_argument("--data-dir", type=Path, default=Path("runs/tasks"))
    parser.add_argument("--projects", type=Path, help="Local projects.json; defaults to DATA_DIR/projects.json")
    parser.add_argument("--workspace-root", type=Path, default=Path("experiments/tasks"))
    parser.add_argument("--runs-root", type=Path, default=Path("runs"), help="Unified requirement/round records")
    parser.add_argument("--project-workspaces", type=Path, default=Path("experiments"), help="Project workspaces; legacy workspace-root is retained")


def build_service(args):
    from repo_agent.core.config import read_dotenv
    database_url = os.environ.get('DATABASE_URL') or read_dotenv().get('DATABASE_URL')
    redis_url = os.environ.get('REDIS_URL') or read_dotenv().get('REDIS_URL')
    if database_url:
        from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
        store = DatabaseTaskStore(args.data_dir, args.runs_root, args.project_workspaces, database_url, redis_url=redis_url)
    else:
        raise TaskError(503, "DATABASE_REQUIRED", "持久任务队列需要配置 DATABASE_URL")
    try:
        return TaskService(store, TaskProjects(args.projects or args.data_dir / "projects.json"),
                           workspace_root=args.workspace_root)
    except Exception:
        store.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4173)
    parser.add_argument('--reload', action='store_true', help='Gracefully reload on development source changes')
    add_storage_arguments(parser)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1..65535')
    if args.reload and not os.environ.get('CODING_AGENT_RELOAD_CHILD'):
        from repo_agent.infrastructure.reload import run_development
        from repo_agent.core.config import ENV_FILE
        raise SystemExit(run_development('repo_agent.backend.app', sys.argv[1:],
                         extra_paths=[ENV_FILE, args.projects or args.data_dir / 'projects.json']))
    try:
        service = build_service(args)
    except TaskError as exc:
        parser.error(exc.message)
    try:
        server = create_server(service, port=args.port)
    except OSError:
        service.close()
        parser.error("Cannot bind local port; choose another --port")
    print(f"Task workbench: http://127.0.0.1:{args.port} (execution POST enqueues; start the independent Worker separately)", flush=True)
    if args.reload:
        from repo_agent.infrastructure.reload import watch_reload
        watch_reload(server.shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
