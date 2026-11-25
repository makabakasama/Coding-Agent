"""Manage workbench accounts from a trusted local terminal."""
import argparse
from contextlib import contextmanager
from getpass import GetPassWarning, getpass
import sys
import warnings

from argon2 import PasswordHasher, Type
import psycopg

from repo_agent.backend.app import storage_urls
from repo_agent.backend.repositories.account_store import AccountStore
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.auth_service import normalize_login, valid_password


def prompt_password(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', GetPassWarning)
            return getpass(prompt)
    except (EOFError, GetPassWarning) as exc:
        raise TaskError(400, 'PASSWORD_PROMPT_REQUIRED', '请从支持隐藏输入的终端输入密码') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('login')
    create.add_argument('--admin', action='store_true')
    for command in ('enable', 'disable'):
        commands.add_parser(command).add_argument('login')
    args = parser.parse_args()
    database_url, _ = storage_urls()
    if not database_url:
        parser.error('请配置 DATABASE_URL')

    @contextmanager
    def connection():
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            yield conn

    try:
        login = normalize_login(args.login)
        if args.command == 'create':
            password = prompt_password('密码: ')
            if not valid_password(password) or len(password) < 3:
                raise TaskError(400, 'INVALID_PASSWORD', '密码至少 3 个字符，最多 1024 UTF-8 字节')
            if password != prompt_password('确认密码: '):
                raise TaskError(400, 'PASSWORD_MISMATCH', '两次输入的密码不同')
            AccountStore(connection).create(login, PasswordHasher(type=Type.ID).hash(password), admin=args.admin)
        else:
            AccountStore(connection).set_enabled(login, args.command == 'enable')
    except TaskError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except psycopg.Error:
        print('账号数据库操作失败，请检查连接配置', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print({'create': '账号已创建', 'enable': '账号已启用', 'disable': '账号已禁用'}[args.command])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
