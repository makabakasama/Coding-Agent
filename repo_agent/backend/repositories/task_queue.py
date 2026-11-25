"""Durable, at-most-once task claims shared by API and independent Worker."""
from contextlib import contextmanager, nullcontext
import hashlib
import uuid

from repo_agent.backend.schemas.task_contract import TaskError


class TaskQueue:
    @staticmethod
    def _view(row):
        result = dict(zip(('executionId', 'status', 'queuedAt', 'startedAt', 'finishedAt', 'reason', 'stale'), row))
        for key in ('queuedAt', 'startedAt', 'finishedAt'):
            result[key] = result[key].isoformat() if result[key] else None
        if result.pop('stale'):
            result.update(status='unknown', reason='WORKER_HEARTBEAT_LOST')
        return result

    def __init__(self, store, audit=None):
        self.store = store
        self.audit = audit
        self.namespace = hashlib.sha256(str(store.root).encode()).hexdigest()
        with store._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS task_dispatch (
                task_id text PRIMARY KEY REFERENCES task_runs(task_id) ON DELETE CASCADE,
                namespace text NOT NULL, execution_id text NOT NULL UNIQUE,
                status text NOT NULL CHECK(status IN ('queued','running','terminated','unknown')),
                queued_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz,
                finished_at timestamptz, worker_id text, heartbeat_at timestamptz, reason text)""")
            conn.execute('CREATE INDEX IF NOT EXISTS task_dispatch_pending ON task_dispatch(namespace,status,queued_at)')
            conn.execute('ALTER TABLE task_dispatch ADD COLUMN IF NOT EXISTS repository_key text')
            conn.execute("ALTER TABLE task_dispatch ADD COLUMN IF NOT EXISTS workspace_mode text NOT NULL DEFAULT 'worktree'")
            conn.execute('ALTER TABLE task_dispatch ADD COLUMN IF NOT EXISTS authorization_proof jsonb')

    def get(self, task_id, *, fresh=False):
        def load():
            with self.store._connection() as conn:
                row = conn.execute("""SELECT execution_id,status,queued_at,started_at,finished_at,reason,
                    status='running' AND (heartbeat_at IS NULL OR heartbeat_at < now()-interval '15 seconds')
                    FROM task_dispatch WHERE task_id=%s AND namespace=%s""", (task_id, self.namespace)).fetchone()
            if row is None:
                return None
            return self._view(row)

        return load() if fresh else self.store.cached_json('dispatch:' + task_id, load, ttl=1)

    def get_many(self, task_ids):
        if not task_ids:
            return {}
        with self.store._connection() as conn:
            rows = conn.execute("""SELECT task_id,execution_id,status,queued_at,started_at,finished_at,reason,
                status='running' AND (heartbeat_at IS NULL OR heartbeat_at < now()-interval '15 seconds')
                FROM task_dispatch WHERE namespace=%s AND task_id=ANY(%s)""", (self.namespace, task_ids)).fetchall()
        return {row[0]: self._view(row[1:]) for row in rows}

    def enqueue(self, task_id, *, repository_key=None, workspace_mode='worktree', connection=None,
                authorization=None, execution_id=None):
        if workspace_mode == 'direct' and repository_key is None:
            raise ValueError('Direct execution requires a repository key')
        with (nullcontext(connection) if connection is not None else self.store._connection()) as conn:
            if self.audit is not None:
                self.audit.lock(conn)
                if self.audit.required(conn) and not authorization:
                    raise TaskError(403, 'AUTH_APPROVAL_REQUIRED', '账号模式必须通过执行审批入队')
            if conn.execute('SELECT task_id FROM task_runs WHERE task_id=%s FOR UPDATE',
                            (task_id,)).fetchone() is None:
                raise TaskError(404, 'TASK_NOT_FOUND', '任务不存在')
            if conn.execute('SELECT 1 FROM task_trash WHERE task_id=%s', (task_id,)).fetchone():
                raise TaskError(409, 'TASK_DELETED', '任务已移入回收站，请先恢复')
            inserted = conn.execute("""INSERT INTO task_dispatch
                (task_id,namespace,execution_id,status,repository_key,workspace_mode,authorization_proof)
                VALUES (%s,%s,%s,'queued',%s,%s,%s) ON CONFLICT(task_id) DO NOTHING RETURNING task_id""",
                         (task_id, self.namespace, execution_id or uuid.uuid4().hex, repository_key, workspace_mode,
                          self.store._json(authorization) if authorization else None)).fetchone()
            namespace, actual_id, actual_proof = conn.execute("""SELECT namespace, execution_id, authorization_proof
                FROM task_dispatch WHERE task_id=%s""", (task_id,)).fetchone()
            if namespace != self.namespace:
                raise TaskError(409, 'QUEUE_LOCATION_MISMATCH', '该轮已在其他任务目录入队，请核对服务启动参数')
            if authorization is not None and (authorization != actual_proof
                    or execution_id is not None and execution_id != actual_id):
                raise TaskError(409, 'AUTH_SCOPE_MISMATCH', '队列已有不同的执行授权或编号')
            if inserted and authorization and self.audit is not None:
                self.audit.job_event(conn, 'task_queued', task_id, actual_id, authorization)

        if connection is None:
            self._invalidate(task_id)
        return actual_id

    def _invalidate(self, *task_ids):
        if self.store.cache:
            self.store.cache.delete(*('dispatch:' + task_id for task_id in task_ids))
            if task_ids:
                self.store.invalidate_lists()

    @contextmanager
    def pool_session(self):
        # Autocommit avoids retaining an idle transaction for the process lifetime.
        with self.store._connection() as conn:
            conn.autocommit = True
            acquired = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',
                                    (self.namespace,)).fetchone()[0]
            if not acquired:
                raise TaskError(409, 'WORKER_ALREADY_RUNNING', '此任务队列已有 Worker 池')
            try:
                yield conn
            finally:
                if not conn.closed:
                    conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (self.namespace,))

    def abandon_claims(self, recover=None):
        if recover is not None:
            with self.store._connection() as conn:
                unfinished = conn.execute("""SELECT task_id,execution_id,worker_id FROM task_dispatch
                    WHERE namespace=%s AND status='running'""", (self.namespace,)).fetchall()
            for task, execution, worker in unfinished:
                reason = recover(task, execution)
                self.finish(task, worker, terminated=reason is not None, reason=reason or 'WORKER_INTERRUPTED')
            return
        with self.store._connection() as conn:
            rows = conn.execute("""UPDATE task_dispatch SET status='unknown',reason='WORKER_INTERRUPTED',finished_at=now()
                WHERE namespace=%s AND status='running'
                RETURNING task_id, execution_id, authorization_proof, worker_id""", (self.namespace,)).fetchall()
            if self.audit is not None:
                for task, execution, proof, worker in rows:
                    if proof:
                        self.audit.job_event(conn, 'task_finished', task, execution, proof, worker, 'WORKER_INTERRUPTED')

        self._invalidate(*(row[0] for row in rows))

    def claim(self, worker_id):
        reason = None
        with self.store._connection() as conn:
            required = False
            if self.audit is not None:
                self.audit.lock(conn)
                required = self.audit.required(conn)
            row = conn.execute("""SELECT d.task_id,d.execution_id,d.repository_key,d.workspace_mode,d.authorization_proof
                FROM task_dispatch d
                WHERE d.namespace=%s AND d.status='queued'
                AND NOT EXISTS (SELECT 1 FROM task_trash trash WHERE trash.task_id=d.task_id) AND NOT EXISTS (
                    SELECT 1 FROM task_dispatch active WHERE active.status='running'
                    AND active.repository_key=d.repository_key
                    AND (active.workspace_mode='direct' OR d.workspace_mode='direct'))
                ORDER BY d.queued_at,d.task_id FOR UPDATE OF d SKIP LOCKED LIMIT 1""", (self.namespace,)).fetchone()
            if row is not None:
                task, execution, repository, mode, proof = row
                if self.audit is not None and (required or proof is not None):
                    reason = self.audit.authorize_task(conn, task, execution, repository, mode, proof)
                if reason:
                    conn.execute("""UPDATE task_dispatch SET status='terminated',reason=%s,finished_at=now()
                        WHERE task_id=%s""", (reason, task))
                    project = conn.execute("""SELECT t.project_id FROM task_runs r
                        JOIN tasks t USING(task_key) WHERE r.task_id=%s""", (task,)).fetchone()
                    context = dict(proof) if isinstance(proof, dict) else {}
                    context['projectId'] = project[0] if project else None
                    self.audit.job_event(conn, 'task_authorization_denied', task, execution, context, worker_id, reason)
                    self.audit.job_event(conn, 'task_finished', task, execution, context, worker_id, reason)
                elif repository:
                    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (row[2],))
                    conflict = conn.execute("""SELECT 1 FROM task_dispatch WHERE repository_key=%s
                        AND status='running' AND (workspace_mode='direct' OR %s='direct') LIMIT 1""",
                                            (row[2], row[3])).fetchone()
                    if conflict:
                        return None
                if not reason:
                    conn.execute("""UPDATE task_dispatch SET status='running',worker_id=%s,
                        started_at=now(),heartbeat_at=now() WHERE task_id=%s""", (worker_id, task))
                    if proof and self.audit is not None:
                        self.audit.job_event(conn, 'task_authorization_passed', task, execution, proof, worker_id)
                        self.audit.job_event(conn, 'task_claimed', task, execution, proof, worker_id)
        if row:
            self._invalidate(row[0])
        return row[:2] if row and not reason else None

    def needs_guarded_preparation(self, task_id, execution_id, worker_id):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT authorization_proof IS NOT NULL AND workspace_mode='worktree' FROM task_dispatch
                WHERE namespace=%s AND task_id=%s AND execution_id=%s AND worker_id=%s AND status='running'""",
                               (self.namespace, task_id, execution_id, worker_id)).fetchone()
        if row is None:
            raise TaskError(409, 'CLAIM_LOST', '任务领取身份已失效')
        return row[0]

    def heartbeat(self, session, worker_id):
        # Use the advisory-lock connection itself so a lost session stops claims.
        rows = session.execute("""UPDATE task_dispatch SET heartbeat_at=now()
            WHERE namespace=%s AND worker_id=%s AND status='running' RETURNING task_id""", (self.namespace, worker_id)).fetchall()
        self._invalidate(*(row[0] for row in rows))

    def finish(self, task_id, worker_id, *, terminated, reason):
        with self.store._connection() as conn:
            row = conn.execute("""UPDATE task_dispatch SET status=%s,reason=%s,finished_at=now()
                WHERE task_id=%s AND namespace=%s AND worker_id=%s AND status='running'
                RETURNING execution_id, authorization_proof""",
                ('terminated' if terminated else 'unknown', reason, task_id, self.namespace, worker_id)).fetchone()
            if row and row[1] and self.audit is not None:
                self.audit.job_event(conn, 'task_finished', task_id, row[0], row[1], worker_id, reason)

        self._invalidate(task_id)

    @contextmanager
    def repository_idle(self, repository_key):
        with self.store._connection() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (repository_key,))
            active = conn.execute("SELECT 1 FROM task_dispatch WHERE repository_key=%s AND status='running' LIMIT 1",
                                  (repository_key,)).fetchone()
            if active:
                raise TaskError(409, 'REPOSITORY_BUSY', '仓库仍有执行中或待核实的领取记录，暂不能撤回')
            yield
