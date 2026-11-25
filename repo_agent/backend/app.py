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


def create_server(service: TaskService, *, port: int = 4173, frontend: Path | None = None, auth=None, access=None):
    if access is not None and auth is None:
        raise ValueError('Project access requires account authentication')
    frontend = frontend or Path(__file__).resolve().parents[2] / "frontend"
    server = TaskHTTPServer(("127.0.0.1", port),
                            partial(TaskHandler, service=service, frontend=frontend, auth=auth, access=access))
    try:
        if access is not None:
            access.audit.enable()
        return server
    except Exception:
        server.server_close()
        raise


def add_storage_arguments(parser):
    parser.add_argument("--data-dir", type=Path, default=Path("runs/tasks"))
    parser.add_argument("--projects", type=Path, help="Local projects.json; defaults to DATA_DIR/projects.json")
    parser.add_argument("--workspace-root", type=Path, default=Path("experiments/tasks"))
    parser.add_argument("--runs-root", type=Path, default=Path("runs"), help="Unified requirement/round records")
    parser.add_argument("--project-workspaces", type=Path, default=Path("experiments"), help="Project workspaces; legacy workspace-root is retained")


def storage_urls():
    from repo_agent.core.config import read_dotenv
    dotenv = read_dotenv()
    return (os.environ.get('DATABASE_URL') or dotenv.get('DATABASE_URL'),
            os.environ.get('REDIS_URL') or dotenv.get('REDIS_URL'))


def build_service(args, *, urls=None):
    database_url, redis_url = urls if urls is not None else storage_urls()
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
    parser.add_argument('--auth', action='store_true', help='Require workbench accounts and Redis sessions')
    add_storage_arguments(parser)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1..65535')
    if args.reload and not os.environ.get('CODING_AGENT_RELOAD_CHILD'):
        from repo_agent.infrastructure.reload import run_development
        from repo_agent.core.config import ENV_FILE
        raise SystemExit(run_development('repo_agent.backend.app', sys.argv[1:],
                         extra_paths=[ENV_FILE, args.projects or args.data_dir / 'projects.json']))
    urls = storage_urls()
    if args.auth and not urls[1]:
        parser.error('账号模式需要配置 REDIS_URL')
    try:
        service = build_service(args, urls=urls)
    except TaskError as exc:
        parser.error(exc.message)
    auth = None
    access = None
    try:
        if args.auth:
            from repo_agent.backend.repositories.account_store import AccountStore
            from repo_agent.backend.repositories.project_access_store import ProjectAccessStore
            from repo_agent.backend.services.auth_service import AuthService
            from repo_agent.backend.services.project_access import ProjectAccess
            try:
                auth = AuthService(AccountStore(service.store._connection), urls[1], urls[0])
                access = ProjectAccess(service, ProjectAccessStore(service.store))
            except TaskError as exc:
                parser.error(exc.message)
            except ValueError:
                parser.error('账号模式的 REDIS_URL 无效')
        try:
            server = create_server(service, port=args.port, auth=auth, access=access)
        except OSError:
            parser.error("Cannot bind local port; choose another --port")
        with server:
            print(f"Task workbench: http://127.0.0.1:{args.port} (execution POST enqueues; start the independent Worker separately)", flush=True)
            if args.reload:
                from repo_agent.infrastructure.reload import watch_reload
                watch_reload(server.shutdown)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        if auth is not None:
            auth.close()
        service.close()


if __name__ == "__main__":
    main()
