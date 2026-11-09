"""Durable, at-most-once task claims shared by API and independent Worker."""
from contextlib import contextmanager
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

    def __init__(self, store):
        self.store = store
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

    def enqueue(self, task_id, *, repository_key=None, workspace_mode='worktree'):
        if workspace_mode == 'direct' and repository_key is None:
            raise ValueError('Direct execution requires a repository key')
        with self.store._connection() as conn:
            conn.execute('SELECT task_id FROM task_runs WHERE task_id=%s FOR UPDATE', (task_id,)).fetchone()
            if conn.execute('SELECT 1 FROM task_trash WHERE task_id=%s', (task_id,)).fetchone():
                raise TaskError(409, 'TASK_DELETED', '任务已移入回收站，请先恢复')
            conn.execute("""INSERT INTO task_dispatch(task_id,namespace,execution_id,status,repository_key,workspace_mode)
                VALUES (%s,%s,%s,'queued',%s,%s) ON CONFLICT(task_id) DO NOTHING""",
                         (task_id, self.namespace, uuid.uuid4().hex, repository_key, workspace_mode))
            namespace = conn.execute('SELECT namespace FROM task_dispatch WHERE task_id=%s', (task_id,)).fetchone()[0]
            if namespace != self.namespace:
                raise TaskError(409, 'QUEUE_LOCATION_MISMATCH', '该轮已在其他任务目录入队，请核对服务启动参数')

        self._invalidate(task_id)

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

    def abandon_claims(self):
        with self.store._connection() as conn:
            rows = conn.execute("""UPDATE task_dispatch SET status='unknown',reason='WORKER_INTERRUPTED',finished_at=now()
                WHERE namespace=%s AND status='running' RETURNING task_id""", (self.namespace,)).fetchall()

        self._invalidate(*(row[0] for row in rows))

    def claim(self, worker_id):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT d.task_id,d.execution_id,d.repository_key,d.workspace_mode FROM task_dispatch d
                WHERE d.namespace=%s AND d.status='queued'
                AND NOT EXISTS (SELECT 1 FROM task_trash trash WHERE trash.task_id=d.task_id) AND NOT EXISTS (
                    SELECT 1 FROM task_dispatch active WHERE active.status='running'
                    AND active.repository_key=d.repository_key
                    AND (active.workspace_mode='direct' OR d.workspace_mode='direct'))
                ORDER BY d.queued_at,d.task_id FOR UPDATE OF d SKIP LOCKED LIMIT 1""", (self.namespace,)).fetchone()
            if row is not None:
                if row[2]:
                    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (row[2],))
                    conflict = conn.execute("""SELECT 1 FROM task_dispatch WHERE repository_key=%s
                        AND status='running' AND (workspace_mode='direct' OR %s='direct') LIMIT 1""",
                                            (row[2], row[3])).fetchone()
                    if conflict:
                        return None
                conn.execute("""UPDATE task_dispatch SET status='running',worker_id=%s,
                    started_at=now(),heartbeat_at=now() WHERE task_id=%s""", (worker_id, row[0]))
        if row:
            self._invalidate(row[0])
        return row[:2] if row else None

    def heartbeat(self, session, worker_id):
        # Use the advisory-lock connection itself so a lost session stops claims.
        rows = session.execute("""UPDATE task_dispatch SET heartbeat_at=now()
            WHERE namespace=%s AND worker_id=%s AND status='running' RETURNING task_id""", (self.namespace, worker_id)).fetchall()
        self._invalidate(*(row[0] for row in rows))

    def finish(self, task_id, worker_id, *, terminated, reason):
        with self.store._connection() as conn:
            conn.execute("""UPDATE task_dispatch SET status=%s,reason=%s,finished_at=now()
                WHERE task_id=%s AND namespace=%s AND worker_id=%s AND status='running'""",
                         ('terminated' if terminated else 'unknown', reason, task_id, self.namespace, worker_id))

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
