"""Local task workbench. Agent executes only after an explicit execution POST."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from urllib.parse import urlsplit, parse_qs
import re

from .task_contract import TaskError, load_json
from .task_projects import TaskProjects
from .task_service import TaskService
from .task_store import TaskStore
from .project_task_store import ProjectTaskStore
from .task_history import REGISTRY


class TaskHandler(BaseHTTPRequestHandler):
    server_version = "CodingAgentTasks/0.3"

    def __init__(self, *args, service, frontend, **kwargs):
        self.service, self.frontend = service, frontend.resolve()
        super().__init__(*args, **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        # Request paths and user-controlled strings are not diagnostics.
        pass

    def _send(self, status, body: bytes, content_type, headers=None):
        self.send_response(status)
        for key, value in {"Content-Type": content_type, "Content-Length": str(len(body)),
                           "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                           "Referrer-Policy": "no-referrer", **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, value, headers=None):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", headers)

    def _local_request(self):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts |= {"127.0.0.1", "localhost"}
        host_values = self.headers.get_all("Host", [])
        if len(host_values) != 1 or host_values[0] not in hosts:
            raise TaskError(403, "ORIGIN_REJECTED", "仅接受本机服务请求")
        if self.command == "POST":
            origins = self.headers.get_all("Origin", [])
            if (origins and origins != ["http://" + host_values[0]]
                    or self.headers.get("Sec-Fetch-Site", "none") not in ("none", "same-origin")):
                raise TaskError(403, "ORIGIN_REJECTED", "不接受跨站写入请求")

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise TaskError(415, "UNSUPPORTED_MEDIA_TYPE", "请求须使用 application/json")
        if self.headers.get("Transfer-Encoding"):
            raise TaskError(400, "INVALID_JSON", "不支持分块请求")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise TaskError(400, "INVALID_JSON", "缺少有效请求体长度")
        normalized_length = lengths[0].lstrip("0") or "0"
        if len(normalized_length) > 5 or int(normalized_length) > 65536:
            raise TaskError(413, "REQUEST_TOO_LARGE", "请求体超过 64 KiB")
        length = int(normalized_length)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete body")
            return load_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError, socket.timeout) as exc:
            raise TaskError(400, "INVALID_JSON", "请求不是有效的 UTF-8 JSON") from exc

    def _dispatch(self):
        self._local_request()
        path = urlsplit(self.path).path
        diagnostics = re.fullmatch(r"/api/tasks/([^/]+)/preparation/diagnostics", path)
        if diagnostics:
            if self.command != "GET":
                raise TaskError(405, "METHOD_NOT_ALLOWED", "准备诊断仅支持读取")
            self._json(200, self.service.preparation.diagnostics(diagnostics[1]))
            return
        organization = re.fullmatch(r"/api/tasks/([^/]+)/organization", path)
        if organization:
            if self.command != "GET":
                raise TaskError(405, "METHOD_NOT_ALLOWED", "关联信息仅支持读取")
            self._json(200, self.service.organization(organization[1]))
            return
        experiment_run = re.fullmatch(
            r"/api/projects/([^/]+)/tasks/([^/]+)/runs(?:/(status))?", path
        )
        if experiment_run:
            project, task, part = experiment_run.groups()
            allowed = "POST" if part is None else "GET"
            if self.command != allowed:
                self._json(405, TaskError(405, "METHOD_NOT_ALLOWED", "实验启动接口方法不支持").response(),
                           {"Allow": allowed})
                return
            if urlsplit(self.path).query:
                raise TaskError(400, "OPEN_PAGE_ERROR", "实验启动接口不接受查询参数")
            if part == "status":
                self._json(200, self.service.experiments.status(project, task))
            else:
                status, body = self.service.experiments.start(project, task, self._body())
                self._json(status, body)
            return
        history = re.fullmatch(r"/api/projects/([^/]+)/tasks(?:/([^/]+)(?:/rounds(?:/([^/]+)(?:/(verifications|artifacts/([^/]+)))?)?)?)?", path)
        if history:
            project, task, identifier, part, artifact_id = history.groups()
            if project not in REGISTRY:
                if urlsplit(self.path).query:
                    raise TaskError(400, "INVALID_CURSOR", "普通需求接口不接受查询参数")
                if self.command == "POST" and task is not None and path.endswith("/rounds"):
                    self._json(201, self.service.new_round(project, task, self._body()))
                elif self.command != "GET":
                    raise TaskError(405, "METHOD_NOT_ALLOWED", "普通需求接口方法不支持")
                elif task is None:
                    self._json(200, self.service.ordinary_tasks(project))
                elif identifier is None and path.endswith("/rounds"):
                    self.service.ordinary_task(project, task)
                    self._json(200, {"rounds": self.service.store.rounds(project, task)})
                elif identifier is None:
                    self._json(200, self.service.ordinary_task(project, task))
                else:
                    raise TaskError(404, "TASK_NOT_FOUND", "请通过轮次的运行编号访问结果")
                return
            if self.command != "GET":
                self._json(405, TaskError(405, "METHOD_NOT_ALLOWED", "实验历史仅支持读取").response(), {"Allow": "GET"})
                return
            project, task, identifier, part, artifact_id = history.groups()
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if (set(query) - ({"offset", "limit"} if artifact_id is not None else set())
                    or any(len(v) != 1 or not re.fullmatch(r"[0-9]{1,9}", v[0]) for v in query.values())):
                raise TaskError(400, "INVALID_CURSOR", "材料分页参数无效")
            limit = int(query.get("limit", ["16000"])[0])
            if not 1 <= limit <= 16000:
                raise TaskError(400, "INVALID_CURSOR", "材料分页长度无效")
            reader = self.service.history
            if task is None:
                body = reader.tasks(project)
            elif identifier is None:
                body = reader.rounds(project, task) if path.endswith("/rounds") else reader.task(project, task)
            elif artifact_id is not None:
                body = reader.artifact(project, task, identifier, artifact_id, int(query.get("offset", ["0"])[0]), limit)
            else:
                body = reader.round(project, task, identifier)
                if part == "verifications":
                    body = {"verifications": body["verifications"]}
            self._json(200, body)
            return
        execution = re.fullmatch(r"/api/tasks/([^/]+)/execution(?:/(events|result|diagnostics|recovery|artifacts/([^/]+)))?", path)
        if execution:
            task_id, part, identifier = execution.groups()
            allowed = "GET, POST" if part in (None, 'recovery') else "GET"
            if self.command not in allowed.split(", "):
                self._json(405, TaskError(405, "METHOD_NOT_ALLOWED", "该接口不支持此方法").response(), {"Allow": allowed})
                return
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            names = {"after", "limit"} if part == "events" else {"offset", "limit"} if identifier is not None else set()
            if set(query) - names or any(len(v) != 1 or not re.fullmatch(r"[0-9]{1,9}", v[0]) for v in query.values()):
                raise TaskError(400, "INVALID_CURSOR", "分页参数无效")
            limit = int(query.get("limit", ["50" if part == "events" else "16000"])[0])
            if names and not 1 <= limit <= (100 if part == "events" else 16000):
                raise TaskError(400, "INVALID_CURSOR", "分页长度无效")
            if part is None:
                status, body = self.service.execution.start(task_id, self._body()) if self.command == "POST" else (200, self.service.execution.get(task_id))
            elif part == "events":
                status, body = 200, self.service.execution.events(task_id, int(query.get("after", ["0"])[0]), limit)
            elif part == "result":
                status, body = 200, self.service.execution.result(task_id)
            elif part == "diagnostics":
                status, body = 200, self.service.execution.diagnostics(task_id)
            elif part == 'recovery':
                status, body = 200, self.service.execution.recovery(task_id, self._body() if self.command == 'POST' else None)
            else:
                status, body = 200, self.service.execution.artifact(task_id, identifier, int(query.get("offset", ["0"])[0]), limit)
            self._json(status, body)
            return
        allowed = None
        preparation_id = None
        if path.startswith("/api/tasks/") and path.endswith("/preparation") and "/" not in path[len("/api/tasks/"):-len("/preparation")]:
            preparation_id = path[len("/api/tasks/"):-len("/preparation")]
            allowed = "GET, POST"
        elif path == "/api/projects":
            allowed = "GET"
        elif path == "/api/tasks":
            allowed = "GET, POST"
        elif path.startswith("/api/tasks/") and "/" not in path[len("/api/tasks/"):]:
            allowed = "GET"
        if allowed:
            if self.command not in allowed.split(", "):
                self._json(405, TaskError(405, "METHOD_NOT_ALLOWED", "该接口不支持此方法").response(), {"Allow": allowed})
            elif preparation_id is not None:
                record = (self.service.prepare(preparation_id, self._body()) if self.command == "POST"
                          else self.service.preparation.query(preparation_id))
                self._json(200, record)
            elif path == "/api/projects":
                self._json(200, {"projects": self.service.history.projects(self.service.projects.list())})
            elif path == "/api/tasks":
                if self.command == "GET":
                    self._json(200, {"tasks": self.service.list()})
                else:
                    record = self.service.create(self._body())
                    self._json(201, record, {"Location": "/api/tasks/" + record["taskId"]})
            else:
                self._json(200, self.service.get(path[len("/api/tasks/"):]))
            return
        assets = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                  "/app.js": ("app.js", "text/javascript"), "/styles.css": ("styles.css", "text/css")}
        if path in assets:
            if self.command not in ("GET", "HEAD"):
                self._json(405, TaskError(405, "METHOD_NOT_ALLOWED", "静态资源只支持读取").response(), {"Allow": "GET, HEAD"})
                return
            name, mime = assets[path]
            asset = self.frontend / name
            if asset.resolve() != asset:
                raise TaskError(404, "ROUTE_NOT_FOUND", "资源不存在")
            try:
                body = asset.read_bytes()
            except OSError as exc:
                raise TaskError(404, "ROUTE_NOT_FOUND", "资源不存在") from exc
            self._send(200, body, mime + "; charset=utf-8")
            return
        raise TaskError(404, "ROUTE_NOT_FOUND", "接口不存在")

    def _handle(self):
        try:
            self._dispatch()
        except TaskError as exc:
            self._json(exc.status, exc.response())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json(500, TaskError(500, "INTERNAL_ERROR", "本机服务内部错误").response())

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _handle


class TaskHTTPServer(ThreadingHTTPServer):
    # Windows otherwise permits two HTTP servers to bind the same port, so a
    # successful bind can still send the browser to an old static server.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(service: TaskService, *, port: int = 4173, frontend: Path | None = None):
    frontend = frontend or Path(__file__).resolve().parent.parent / "frontend"
    return TaskHTTPServer(("127.0.0.1", port), partial(TaskHandler, service=service, frontend=frontend))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--data-dir", type=Path, default=Path("runs/tasks"))
    parser.add_argument("--projects", type=Path, help="Local projects.json; defaults to DATA_DIR/projects.json")
    parser.add_argument("--workspace-root", type=Path, default=Path("experiments/tasks"))
    parser.add_argument("--runs-root", type=Path, default=Path("runs"), help="Unified requirement/round records")
    parser.add_argument("--project-workspaces", type=Path, default=Path("experiments"), help="Project workspaces; legacy workspace-root is retained")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    service = TaskService(ProjectTaskStore(args.data_dir, args.runs_root, args.project_workspaces),
                          TaskProjects(args.projects or args.data_dir / "projects.json"), workspace_root=args.workspace_root)
    try:
        server = create_server(service, port=args.port)
    except OSError:
        parser.error("Cannot bind local port; choose another --port")
    print(f"Task workbench: http://127.0.0.1:{args.port} (Agent starts only via explicit execution POST)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
