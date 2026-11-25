"""Command-line client for the same local task API used by the web workbench."""
from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import sys
from urllib.parse import quote

from repo_agent.backend.routers.auth_http import CSRF_HEADER, cookie_name
from repo_agent.backend.schemas.task_contract import TaskError, load_json


CREDENTIAL_SERVICE = 'Coding-Agent workbench'


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--port', type=int, default=4176, help='Local workbench port (default: 4176)')
    commands = cli.add_subparsers(dest='command', required=True)
    commands.add_parser('login').add_argument('login')
    commands.add_parser('whoami')
    commands.add_parser('logout')
    enable = commands.add_parser('enable-project')
    enable.add_argument('project')
    enable.add_argument('maintainer')
    commands.add_parser('members').add_argument('project')
    member = commands.add_parser('set-member')
    member.add_argument('project')
    member.add_argument('login')
    member.add_argument('role', choices=('observer', 'developer', 'maintainer', 'remove'))
    requests = commands.add_parser('requests')
    requests.add_argument('project')
    requests.add_argument('--status', choices=('pending', 'approved', 'rejected'), default='pending')
    for name in ('request-status', 'approve', 'reject'):
        command = commands.add_parser(name)
        command.add_argument('project')
        command.add_argument('request')
    for name in ('projects', 'list'):
        commands.add_parser(name)
    create = commands.add_parser('create', help='Save a requirement and first round; does not run the model')
    create.add_argument('--file', type=Path, required=True)
    requirements = commands.add_parser('requirements')
    requirements.add_argument('project')
    for name in ('rounds', 'new-round'):
        command = commands.add_parser(name)
        command.add_argument('project')
        command.add_argument('requirement')
    for name in ('show', 'organization', 'prepare', 'diagnostics', 'status', 'start', 'result', 'events'):
        command = commands.add_parser(name, help='Submit for approval in account mode' if name == 'start' else None)
        command.add_argument('run_id')
        if name == 'events':
            command.add_argument('--after', type=int, default=0)
            command.add_argument('--limit', type=int, default=50)
    return cli


def request_for(args):
    segment = lambda value: quote(value, safe='')
    command = args.command
    if command == 'whoami':
        return 'GET', '/api/auth/me', None
    if command == 'logout':
        return 'POST', '/api/auth/logout', None
    if command == 'projects':
        return 'GET', '/api/projects', None
    if command == 'list':
        return 'GET', '/api/tasks', None
    if command == 'create':
        return 'POST', '/api/tasks', load_json(args.file.read_text(encoding='utf-8-sig'))
    if command in ('enable-project', 'members', 'set-member', 'requests', 'request-status', 'approve', 'reject'):
        root = '/api/projects/' + segment(args.project)
        if command == 'enable-project':
            return 'POST', root + '/enable', {'maintainer': args.maintainer}
        if command == 'members':
            return 'GET', root + '/members', None
        if command == 'set-member':
            return 'POST', root + '/members', {'login': args.login, 'role': None if args.role == 'remove' else args.role}
        requests = root + '/execution-requests'
        if command == 'requests':
            return 'GET', requests + '?status=' + args.status, None
        path = requests + '/' + segment(args.request)
        return ('GET', path, None) if command == 'request-status' else ('POST', path, {'decision': command})
    if command in ('requirements', 'rounds', 'new-round'):
        path = '/api/projects/' + segment(args.project) + '/tasks'
        if command != 'requirements':
            path += '/' + segment(args.requirement) + '/rounds'
        return ('POST', path, {}) if command == 'new-round' else ('GET', path, None)
    path = '/api/tasks/' + segment(args.run_id)
    suffix = {'show': '', 'organization': '/organization', 'prepare': '/preparation',
              'diagnostics': '/preparation/diagnostics', 'status': '/execution',
              'start': '/execution', 'result': '/execution/result',
              'events': f'/execution/events?after={getattr(args, "after", 0)}&limit={getattr(args, "limit", 50)}'}
    return ('POST' if command in ('prepare', 'start') else 'GET', path + suffix[command],
            {} if command in ('prepare', 'start') else None)


def exchange(connection, method, path, data=None, headers=None):
    headers = dict(headers or {})
    body = None
    if data is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    return response.status, load_json(response.read()), response.getheader('Set-Cookie')


def report(status, result):
    failed = status >= 400
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr if failed else sys.stdout)
    return 1 if failed else 0


def credential_store(port):
    import keyring

    try:
        return keyring, str(port), keyring.get_password(CREDENTIAL_SERVICE, str(port))
    except Exception as exc:
        # Native credential providers can raise OS-specific exceptions.
        raise TaskError(400, 'CLI_CREDENTIAL_STORE_UNAVAILABLE', '系统凭据存储不可用') from exc


def login(connection, args):
    from repo_agent.cli.account_admin import prompt_password

    keyring, key, previous = credential_store(args.port)
    name = cookie_name(args.port)
    if previous:
        status, identity, _ = exchange(connection, 'GET', '/api/auth/me', headers={'Cookie': f'{name}={previous}'})
        if status == 200 and identity['authRequired']:
            raise TaskError(409, 'CLI_ALREADY_LOGGED_IN', '请先退出当前 CLI 会话')
        if status != 401:
            if status == 200:
                raise TaskError(404, 'AUTH_DISABLED', '此工作台未启用账号模式')
            return report(status, identity)
    password = prompt_password('密码: ')
    status, result, cookie = exchange(connection, 'POST', '/api/auth/login',
                                      {'login': args.login, 'password': password})
    if status != 200:
        return report(status, result)
    try:
        parsed = SimpleCookie(cookie or '')
        token = parsed[name].value
        csrf = result.pop('csrfToken')
    except (CookieError, KeyError, TypeError) as exc:
        raise ValueError('Invalid login response') from exc
    try:
        keyring.set_password(CREDENTIAL_SERVICE, key, token)
    except Exception as exc:
        try:
            exchange(connection, 'POST', '/api/auth/logout', headers={'Cookie': f'{name}={token}', CSRF_HEADER: csrf})
        except (OSError, HTTPException, ValueError):
            pass
        raise TaskError(400, 'CLI_CREDENTIAL_STORE_UNAVAILABLE', '系统凭据存储不可用，未保存会话') from exc
    return report(status, result)


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if not 1 <= args.port <= 65535:
        cli.error('port must be 1..65535')
    connection = HTTPConnection('127.0.0.1', args.port, timeout=180)
    method = 'POST' if args.command == 'login' else 'GET'
    try:
        if args.command == 'login':
            return login(connection, args)
        try:
            method, path, data = request_for(args)
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            print(json.dumps({'error': {'code': 'CLI_INPUT_ERROR', 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
            return 2
        headers = {}
        try:
            keyring, key, token = credential_store(args.port)
        except (ImportError, TaskError):
            if args.command == 'logout':
                raise TaskError(400, 'CLI_CREDENTIAL_STORE_UNAVAILABLE', '系统凭据存储不可用') from None
            token = None
        if token:
            headers['Cookie'] = f'{cookie_name(args.port)}={token}'
            if method == 'POST':
                status, identity, _ = exchange(connection, 'GET', '/api/auth/me', headers=headers)
                if status != 200:
                    return report(status, identity)
                if identity.get('authRequired'):
                    headers[CSRF_HEADER] = identity['csrfToken']
        status, result, _ = exchange(connection, method, path, data, headers)
        if args.command == 'whoami' and status == 200:
            result.pop('csrfToken', None)
        if args.command == 'logout' and status == 200 and token:
            try:
                keyring.delete_password(CREDENTIAL_SERVICE, key)
            except Exception:
                return report(status, {**result, 'warning': '服务端会话已撤销，系统凭据未能清理'})
        return report(status, result)
    except TaskError as exc:
        return report(exc.status, exc.response())
    except ImportError:
        return report(400, {'error': {'code': 'CLI_CREDENTIAL_STORE_UNAVAILABLE',
                                      'message': '请安装工作台依赖并使用系统凭据存储'}})
    except KeyboardInterrupt:
        return 130
    except (OSError, HTTPException, ValueError, UnicodeError, RecursionError):
        code = 'COMMAND_OUTCOME_UNKNOWN' if method == 'POST' else 'SERVER_UNAVAILABLE'
        message = ('请求结果无法确认，请查询记录，勿直接重复提交。' if method == 'POST'
                   else '无法读取工作台响应，请确认本地服务及端口。')
        print(json.dumps({'error': {'code': code, 'message': message}}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
