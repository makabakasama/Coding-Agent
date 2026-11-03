"""Command-line client for the same local task API used by the web workbench."""
from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import sys
from urllib.parse import quote

from .task_contract import load_json


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--port', type=int, default=4176, help='Local workbench port (default: 4176)')
    commands = cli.add_subparsers(dest='command', required=True)
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
        command = commands.add_parser(name, help='Start the configured model' if name == 'start' else None)
        command.add_argument('run_id')
        if name == 'events':
            command.add_argument('--after', type=int, default=0)
            command.add_argument('--limit', type=int, default=50)
    return cli


def request_for(args):
    segment = lambda value: quote(value, safe='')
    command = args.command
    if command == 'projects':
        return 'GET', '/api/projects', None
    if command == 'list':
        return 'GET', '/api/tasks', None
    if command == 'create':
        return 'POST', '/api/tasks', load_json(args.file.read_text(encoding='utf-8-sig'))
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


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if not 1 <= args.port <= 65535:
        cli.error('port must be 1..65535')
    try:
        method, path, data = request_for(args)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        print(json.dumps({'error': {'code': 'CLI_INPUT_ERROR', 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    connection = HTTPConnection('127.0.0.1', args.port, timeout=180)
    try:
        connection.request(method, path, body=None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8'),
                           headers={'Content-Type': 'application/json'} if data is not None else {})
        response = connection.getresponse()
        result = load_json(response.read())
        failed = response.status >= 400
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr if failed else sys.stdout)
        return 1 if failed else 0
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
