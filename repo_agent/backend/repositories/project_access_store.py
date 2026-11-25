"""Project membership and pending execution requests in PostgreSQL."""
from hashlib import sha256
from pathlib import Path
import uuid

from repo_agent.backend.schemas.task_contract import TaskError


ROLES = {'observer': 1, 'developer': 2, 'maintainer': 3}


def missing_project():
    return TaskError(404, 'PROJECT_NOT_FOUND', '项目或项目资源不存在')


class ProjectAccessStore:
    def __init__(self, store):
        self.store = store
        self.audit = None
        self.namespace = sha256(str(store.root).encode()).hexdigest()
        with store._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_projects (
                namespace text NOT NULL, project_id text NOT NULL, source_path text NOT NULL,
                PRIMARY KEY (namespace, project_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_project_members (
                namespace text NOT NULL, project_id text NOT NULL,
                account_id text NOT NULL REFERENCES workbench_accounts(account_id),
                role text NOT NULL CHECK (role IN ('observer', 'developer', 'maintainer')),
                PRIMARY KEY (namespace, project_id, account_id),
                FOREIGN KEY (namespace, project_id) REFERENCES workbench_projects(namespace, project_id)
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS workbench_project_members_account
                ON workbench_project_members(namespace, account_id, project_id)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_execution_requests (
                namespace text NOT NULL, request_id text NOT NULL, project_id text NOT NULL,
                requester_id text NOT NULL REFERENCES workbench_accounts(account_id),
                kind text NOT NULL CHECK (kind IN ('task', 'chat')),
                target_id text NOT NULL, client_key text NOT NULL, snapshot jsonb NOT NULL,
                status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'rejected', 'approved')),
                created_at timestamptz NOT NULL DEFAULT now(),
                decided_at timestamptz, decided_by text REFERENCES workbench_accounts(account_id),
                execution jsonb,
                PRIMARY KEY (namespace, request_id),
                UNIQUE (namespace, kind, target_id, client_key),
                FOREIGN KEY (namespace, project_id) REFERENCES workbench_projects(namespace, project_id)
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS workbench_execution_requests_project
                ON workbench_execution_requests(namespace, project_id, created_at DESC, request_id DESC)""")

    def memberships(self, account_id):
        with self.store._connection() as conn:
            return dict(conn.execute("""SELECT project_id, role FROM workbench_project_members
                WHERE namespace=%s AND account_id=%s""", (self.namespace, account_id)).fetchall())

    def enabled_projects(self):
        with self.store._connection() as conn:
            return {row[0] for row in conn.execute("""SELECT project_id FROM workbench_projects
                WHERE namespace=%s""", (self.namespace,))}

    def role(self, conn, project, account_id):
        row = conn.execute("""SELECT role FROM workbench_project_members
            WHERE namespace=%s AND project_id=%s AND account_id=%s""",
                           (self.namespace, project, account_id)).fetchone()
        return row[0] if row else None

    def project(self, conn, project, *, lock=False):
        suffix = ' FOR UPDATE' if lock else ''
        row = conn.execute("""SELECT source_path FROM workbench_projects
            WHERE namespace=%s AND project_id=%s""" + suffix, (self.namespace, project)).fetchone()
        if row is None:
            raise missing_project()
        return row[0]

    @staticmethod
    def active_account(conn, login):
        row = conn.execute("""SELECT account_id FROM workbench_accounts
            WHERE login=%s AND enabled FOR SHARE""", (login,)).fetchone()
        if row is None:
            raise TaskError(404, 'ACCOUNT_NOT_FOUND', '可用账号不存在')
        return row[0]

    def enable(self, project, path, maintainer):
        with self.store._connection() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                         ('workbench-project-enable:' + self.namespace,))
            existing = conn.execute("""SELECT project_id, source_path FROM workbench_projects
                WHERE namespace=%s""", (self.namespace,)).fetchall()
            root = Path(path)
            for identifier, source in existing:
                if identifier == project:
                    raise TaskError(409, 'PROJECT_ALREADY_ENABLED', '该项目已启用成员授权')
                other = Path(source)
                if root.is_relative_to(other) or other.is_relative_to(root):
                    raise TaskError(409, 'PROJECT_SOURCE_OVERLAP', '仓库路径与已启用项目重叠')
            row = conn.execute("""INSERT INTO workbench_projects (namespace, project_id, source_path)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING project_id""",
                               (self.namespace, project, path)).fetchone()
            if row is None:
                raise TaskError(409, 'PROJECT_ALREADY_ENABLED', '该项目已启用成员授权')
            account = self.active_account(conn, maintainer)
            conn.execute("""INSERT INTO workbench_project_members VALUES (%s,%s,%s,'maintainer')""",
                         (self.namespace, project, account))

    def members(self, project):
        with self.store._connection() as conn:
            self.project(conn, project)
            rows = conn.execute("""SELECT a.login, m.role, a.enabled
                FROM workbench_project_members m JOIN workbench_accounts a USING(account_id)
                WHERE m.namespace=%s AND m.project_id=%s ORDER BY a.login""",
                                (self.namespace, project)).fetchall()
        return [{'login': login, 'role': role, 'enabled': enabled} for login, role, enabled in rows]

    def change_member(self, project, actor, login, role):
        with self.store._connection() as conn:
            if self.audit is not None:
                self.audit.lock(conn)
            self.project(conn, project, lock=True)
            active = conn.execute("""SELECT admin FROM workbench_accounts WHERE account_id=%s AND enabled
                FOR SHARE""", (actor.account_id,)).fetchone()
            if active is None or not active[0] and self.role(conn, project, actor.account_id) != 'maintainer':
                raise TaskError(403, 'ROLE_REQUIRED', '此操作需要项目维护者')
            row = conn.execute("""SELECT account_id, enabled FROM workbench_accounts
                WHERE login=%s FOR SHARE""", (login,)).fetchone()
            if row is None or (role is not None and not row[1]):
                raise TaskError(404, 'ACCOUNT_NOT_FOUND', '账号不存在或不可用')
            account, _ = row
            key = (self.namespace, project, account)
            if role is None:
                conn.execute("""DELETE FROM workbench_project_members
                    WHERE namespace=%s AND project_id=%s AND account_id=%s""", key)
            else:
                conn.execute("""INSERT INTO workbench_project_members VALUES (%s,%s,%s,%s)
                    ON CONFLICT (namespace, project_id, account_id) DO UPDATE SET role=EXCLUDED.role""",
                             (*key, role))
            maintainer = conn.execute("""SELECT 1
                FROM workbench_project_members m JOIN workbench_accounts a USING(account_id)
                WHERE m.namespace=%s AND m.project_id=%s AND m.role='maintainer' AND a.enabled
                LIMIT 1""", (self.namespace, project)).fetchone()
            if maintainer is None:
                raise TaskError(409, 'LAST_MAINTAINER', '项目必须保留至少一位可用维护者')

    def create_request(self, conn, project, requester, kind, target, key, snapshot):
        request_id = uuid.uuid4().hex
        row = conn.execute("""INSERT INTO workbench_execution_requests
            (namespace, request_id, project_id, requester_id, kind, target_id, client_key, snapshot)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING request_id""",
                           (self.namespace, request_id, project, requester, kind, target, key,
                            self.store._json(snapshot))).fetchone()
        if row is None:
            row = conn.execute("""SELECT request_id, requester_id, snapshot FROM workbench_execution_requests
                WHERE namespace=%s AND kind=%s AND target_id=%s AND client_key=%s""",
                               (self.namespace, kind, target, key)).fetchone()
            if row is None or row[1:] != (requester, snapshot):
                raise TaskError(409, 'REQUEST_ALREADY_EXISTS', '该操作已有不同的执行申请')
        return self.request(conn, project, row[0])

    def previous_request(self, conn, project, kind, target, key):
        row = conn.execute("""SELECT request_id FROM workbench_execution_requests
            WHERE namespace=%s AND project_id=%s AND kind=%s AND target_id=%s AND client_key=%s""",
                           (self.namespace, project, kind, target, key)).fetchone()
        return self.request(conn, project, row[0]) if row else None

    @staticmethod
    def view(row):
        (request_id, project, requester, kind, target, snapshot, status, created, decided, approver,
         execution, requester_login, approver_login) = row
        return {'requestId': request_id, 'projectId': project, 'requesterId': requester,
                'kind': kind, 'targetId': target, 'snapshot': snapshot, 'status': status,
                'createdAt': created.isoformat(), 'decidedAt': decided.isoformat() if decided else None,
                'decidedBy': approver, 'execution': execution, 'requesterLogin': requester_login,
                'decidedByLogin': approver_login}

    def request(self, conn, project, request_id, *, lock=False):
        suffix = ' FOR UPDATE' if lock else ''
        row = conn.execute("""SELECT request_id, project_id, requester_id, kind, target_id, snapshot,
            status, created_at, decided_at, decided_by, execution,
            (SELECT login FROM workbench_accounts WHERE account_id=requester_id),
            (SELECT login FROM workbench_accounts WHERE account_id=decided_by)
            FROM workbench_execution_requests
            WHERE namespace=%s AND project_id=%s AND request_id=%s""" + suffix,
                           (self.namespace, project, request_id)).fetchone()
        if row is None:
            raise TaskError(404, 'EXECUTION_REQUEST_NOT_FOUND', '执行申请不存在')
        return self.view(row)

    def requests(self, project, status):
        with self.store._connection() as conn:
            rows = conn.execute("""SELECT request_id, project_id, requester_id, kind, target_id, snapshot,
                status, created_at, decided_at, decided_by, execution,
                (SELECT login FROM workbench_accounts WHERE account_id=requester_id),
                (SELECT login FROM workbench_accounts WHERE account_id=decided_by)
                FROM workbench_execution_requests
                WHERE namespace=%s AND project_id=%s AND status=%s
                ORDER BY created_at DESC, request_id DESC LIMIT 50""", (self.namespace, project, status)).fetchall()
        return [self.view(row) for row in rows]

    def task_request(self, project, task_id):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT request_id, status, kind FROM workbench_execution_requests
                WHERE namespace=%s AND project_id=%s AND ((kind='task' AND target_id=%s AND client_key='execution')
                OR (kind='chat' AND status='approved' AND execution->>'taskId'=%s))
                ORDER BY kind LIMIT 1""", (self.namespace, project, task_id, task_id)).fetchone()
        return {'requestId': row[0], 'status': row[1], 'projectId': project, 'kind': row[2]} if row else None

    def decide(self, conn, project, request_id, actor, status, execution):
        conn.execute("""UPDATE workbench_execution_requests
            SET status=%s, decided_by=%s, decided_at=now(), execution=%s
            WHERE namespace=%s AND project_id=%s AND request_id=%s""",
                     (status, actor, self.store._json(execution) if execution else None,
                      self.namespace, project, request_id))
        return self.request(conn, project, request_id)
