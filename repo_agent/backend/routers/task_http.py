"""Local task workbench. Agent executes only after an explicit execution POST."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit, parse_qs
import re

from repo_agent.backend.schemas.task_contract import TaskError, load_json
from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.repositories.task_history import REGISTRY


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
                           "Connection": "close",
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
        if path == '/api/conversations' or path.startswith('/api/conversations/'):
            from repo_agent.backend.routers.chat_http import dispatch_chat
            dispatch_chat(self, path)
            return
        if path == '/api/local-files':
            if self.command != 'GET':
                raise TaskError(405, 'METHOD_NOT_ALLOWED', '文件浏览仅支持读取')
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if set(query) - {'path'} or any(len(values) != 1 for values in query.values()):
                raise TaskError(400, 'INVALID_TARGET', '浏览路径无效')
            root = Path(__file__).resolve().parents[4]
            requested = query.get('path', [str(root)])[0]
            try:
                directory = Path(requested).resolve(strict=True)
                relative = directory.relative_to(root)
                if '.git' in relative.parts or not directory.is_dir():
                    raise ValueError('Not a directory')
            except (OSError, ValueError) as exc:
                raise TaskError(400, 'INVALID_TARGET', '只能浏览工作目录内的文件夹') from exc
            from repo_agent.core.tools import TEXT_EXT
            try:
                entries = sorted(
                    ({'name': item.name, 'path': str(item), 'type': 'directory' if item.is_dir() else 'file'}
                     for item in directory.iterdir()
                     if item.name != '.git' and not item.is_symlink()
                     and item.resolve().is_relative_to(root)
                     and (item.is_dir() or item.is_file() and item.suffix.lower() in TEXT_EXT)),
                    key=lambda item: (item['type'] != 'directory', item['name'].lower()),
                )
            except OSError as exc:
                raise TaskError(400, 'INVALID_TARGET', '无法读取该文件夹') from exc
            self._json(200, {'root': str(root), 'path': str(directory),
                             'parent': str(directory.parent) if directory != root else None,
                             'entries': entries[:500], 'truncated': len(entries) > 500})
            return
        purge = re.fullmatch(r'/api/tasks/([^/]+)/purge', path)
        if purge:
            if self.command not in ('GET', 'POST'):
                raise TaskError(405, 'METHOD_NOT_ALLOWED', '彻底删除接口支持 GET 和 POST')
            value = (self.service.purge.get(purge[1]) if self.command == 'GET'
                     else self.service.purge.apply(purge[1], self._body()))
            self._json(200, value)
            return
        lifecycle = re.fullmatch(r'/api/tasks/([^/]+)/lifecycle', path)
        if lifecycle:
            if self.command not in ('GET', 'POST'):
                raise TaskError(405, 'METHOD_NOT_ALLOWED', '本轮删除状态接口支持 GET 和 POST')
            value = (self.service.lifecycle.get(lifecycle[1]) if self.command == 'GET'
                     else self.service.lifecycle.change(lifecycle[1], self._body()))
            self._json(200, value)
            return
        if path == '/api/trash':
            if self.command != 'GET':
                raise TaskError(405, 'METHOD_NOT_ALLOWED', '回收站仅支持读取')
            self._json(200, {'tasks': self.service.list(deleted=True)})
            return
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
        execution = re.fullmatch(r"/api/tasks/([^/]+)/execution(?:/(events|result|diagnostics|recovery|undo|artifacts/([^/]+)))?", path)
        if execution:
            task_id, part, identifier = execution.groups()
            allowed = "GET, POST" if part in (None, 'recovery', 'undo') else "GET"
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
            elif part == 'undo':
                status, body = 200, self.service.undo.apply(task_id, self._body()) if self.command == 'POST' else self.service.undo.get(task_id)
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
                  "/app.js": ("app.js", "text/javascript"), "/chat.js": ("chat.js", "text/javascript"),
                  "/styles.css": ("styles.css", "text/css")}
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
    # Reload waits for in-flight task saves and worktree preparation to return.
    daemon_threads = False
    # Windows otherwise permits two HTTP servers to bind the same port, so a
    # successful bind can still send the browser to an old static server.
    allow_reuse_address = False

    def shutdown_request(self, request):
        # HTTP/1.0 closes each connection. After an early rejection, unread
        # upload data can turn an immediate full close into a TCP reset and
        # discard the response. Half-close first, then linger within both caps.
        try:
            request.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 0.25
            remaining = 65537  # Include the first byte beyond the accepted 64 KiB body.
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                request.settimeout(timeout)
                chunk = request.recv(min(8192, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # Peer disconnects and the bounded read timeout still require close.
            pass
        finally:
            self.close_request(request)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


