"""PostgreSQL accounts and durable session revocations."""
from dataclasses import dataclass
import uuid

from repo_agent.backend.schemas.task_contract import TaskError


@dataclass(frozen=True)
class Account:
    account_id: str
    login: str
    password_hash: str
    admin: bool
    enabled: bool
    session_version: int


class AccountStore:
    def __init__(self, connection):
        self.connection = connection
        with self.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_accounts (
                account_id text PRIMARY KEY,
                login text NOT NULL UNIQUE,
                password_hash text NOT NULL,
                admin boolean NOT NULL DEFAULT false,
                enabled boolean NOT NULL DEFAULT true,
                session_version bigint NOT NULL DEFAULT 1
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_revoked_sessions (
                token_digest text PRIMARY KEY,
                expires_at bigint NOT NULL
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS workbench_revoked_sessions_expiry
                ON workbench_revoked_sessions (expires_at)""")

    def by_login(self, login):
        with self.connection() as conn:
            row = conn.execute("""SELECT account_id, login, password_hash, admin, enabled, session_version
                FROM workbench_accounts WHERE login = %s""", (login,)).fetchone()
        return Account(*row) if row else None

    def active_identity(self, account_id, session_version, token_digest):
        with self.connection() as conn:
            return conn.execute("""SELECT login, admin FROM workbench_accounts
                WHERE account_id = %s AND enabled AND session_version = %s
                AND NOT EXISTS (SELECT 1 FROM workbench_revoked_sessions WHERE token_digest = %s)""",
                (account_id, session_version, token_digest)).fetchone()

    def revoke_session(self, token_digest, expires_at, now):
        with self.connection() as conn:
            conn.execute('DELETE FROM workbench_revoked_sessions WHERE expires_at <= %s', (now,))
            if expires_at > now:
                conn.execute("""INSERT INTO workbench_revoked_sessions (token_digest, expires_at)
                    VALUES (%s, %s) ON CONFLICT (token_digest) DO NOTHING""", (token_digest, expires_at))

    def create(self, login, password_hash, *, admin=False):
        with self.connection() as conn:
            row = conn.execute("""INSERT INTO workbench_accounts (account_id, login, password_hash, admin)
                VALUES (%s, %s, %s, %s) ON CONFLICT (login) DO NOTHING RETURNING account_id""",
                (uuid.uuid4().hex, login, password_hash, admin)).fetchone()
        if row is None:
            raise TaskError(409, 'ACCOUNT_EXISTS', '登录名已存在')

    def set_enabled(self, login, enabled):
        with self.connection() as conn:
            row = conn.execute("""UPDATE workbench_accounts
                SET session_version = session_version + CASE WHEN enabled AND NOT %s THEN 1 ELSE 0 END,
                    enabled = %s
                WHERE login = %s RETURNING account_id""", (enabled, enabled, login)).fetchone()
        if row is None:
            raise TaskError(404, 'ACCOUNT_NOT_FOUND', '账号不存在')
