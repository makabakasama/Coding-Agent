"""Shared account and session HTTP contract for the workbench and CLI."""
import secrets

from repo_agent.backend.schemas.task_contract import TaskError


COOKIE_PREFIX = 'coding_agent_session_'
CSRF_HEADER = 'X-CSRF-Token'
AUTH_METHODS = {'/api/auth/login': 'POST', '/api/auth/me': 'GET', '/api/auth/logout': 'POST'}


def cookie_name(port):
    return f'{COOKIE_PREFIX}{port}'


def session_token(handler):
    name = cookie_name(handler.server.server_address[1])
    tokens = []
    for header in handler.headers.get_all('Cookie', []):
        for cookie in header.split(';'):
            key, separator, value = cookie.strip().partition('=')
            if separator and key == name:
                tokens.append(value)
    return tokens[0] if len(tokens) == 1 else None


def session_cookie(handler, token, max_age):
    # The HTTP server is loopback-only. Bind the cookie to the host and
    # distinguish local workbench ports by cookie name.
    name = cookie_name(handler.server.server_address[1])
    return f'{name}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict'


def check_csrf(handler, identity):
    values = handler.headers.get_all(CSRF_HEADER, [])
    if len(values) != 1 or not values[0].isascii() or not secrets.compare_digest(values[0], identity.csrf):
        raise TaskError(403, 'CSRF_REJECTED', '请求校验失败，请刷新工作台')


def authenticate_request(handler):
    if handler.auth is None:
        return None
    identity = handler.auth.authenticate(session_token(handler))
    if handler.command not in ('GET', 'HEAD', 'OPTIONS'):
        check_csrf(handler, identity)
    return identity


def require_admin(handler):
    if handler.auth is None:
        return
    identity = handler.auth.authenticate(session_token(handler))
    if not identity.admin:
        raise TaskError(403, 'ADMIN_REQUIRED', '此操作需要管理员账号')
    if handler.command not in ('GET', 'HEAD', 'OPTIONS'):
        check_csrf(handler, identity)


def dispatch_auth(handler, path):
    method = AUTH_METHODS[path]
    if handler.command != method:
        handler._json(405, TaskError(405, 'METHOD_NOT_ALLOWED', '该接口不支持此方法').response(),
                      {'Allow': method})
        return
    auth = handler.auth
    if auth is None:
        if path == '/api/auth/me':
            handler._json(200, {'authRequired': False})
            return
        raise TaskError(404, 'AUTH_DISABLED', '此工作台未启用账号模式')
    if path == '/api/auth/login':
        from repo_agent.backend.services.auth_service import SESSION_SECONDS
        body = handler._body()
        if not isinstance(body, dict) or set(body) != {'login', 'password'}:
            raise TaskError(400, 'INVALID_LOGIN_REQUEST', '登录请求须包含登录名和密码')
        token, identity = auth.login(body['login'], body['password'])
        handler._json(200, auth_response(handler, identity),
                      {'Set-Cookie': session_cookie(handler, token, SESSION_SECONDS)})
        return
    token = session_token(handler)
    identity = auth.authenticate(token)
    if path == '/api/auth/logout':
        check_csrf(handler, identity)
        auth.logout(token, identity)
        handler._json(200, {'loggedOut': True}, {'Set-Cookie': session_cookie(handler, '', 0)})
        return
    handler._json(200, auth_response(handler, identity))


def auth_response(handler, identity):
    response = identity.response()
    if getattr(handler, 'access', None) is not None:
        response['projectAccess'] = True
    return response
