"""Durable, at-most-once task claims shared by API and independent Worker."""
from contextlib import contextmanager
import hashlib
import uuid

from repo_agent.backend.schemas.task_contract import TaskError


class TaskQueue:
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

    def get(self, task_id):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT execution_id,status,queued_at,started_at,finished_at,reason,
                status='running' AND (heartbeat_at IS NULL OR heartbeat_at < now()-interval '15 seconds')
                FROM task_dispatch WHERE task_id=%s AND namespace=%s""", (task_id, self.namespace)).fetchone()
        if row is None:
            return None
        result = dict(zip(('executionId', 'status', 'queuedAt', 'startedAt', 'finishedAt', 'reason', 'stale'), row))
        for key in ('queuedAt', 'startedAt', 'finishedAt'):
            result[key] = result[key].isoformat() if result[key] else None
        if result.pop('stale'):
            result.update(status='unknown', reason='WORKER_HEARTBEAT_LOST')
        return result

    def enqueue(self, task_id):
        with self.store._connection() as conn:
            conn.execute("""INSERT INTO task_dispatch(task_id,namespace,execution_id,status)
                VALUES (%s,%s,%s,'queued') ON CONFLICT(task_id) DO NOTHING""",
                         (task_id, self.namespace, uuid.uuid4().hex))
            namespace = conn.execute('SELECT namespace FROM task_dispatch WHERE task_id=%s', (task_id,)).fetchone()[0]
            if namespace != self.namespace:
                raise TaskError(409, 'QUEUE_LOCATION_MISMATCH', '该轮已在其他任务目录入队，请核对服务启动参数')

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
            conn.execute("""UPDATE task_dispatch SET status='unknown',reason='WORKER_INTERRUPTED',finished_at=now()
                WHERE namespace=%s AND status='running'""", (self.namespace,))

    def claim(self, worker_id):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT task_id,execution_id FROM task_dispatch
                WHERE namespace=%s AND status='queued' ORDER BY queued_at,task_id
                FOR UPDATE SKIP LOCKED LIMIT 1""", (self.namespace,)).fetchone()
            if row is not None:
                conn.execute("""UPDATE task_dispatch SET status='running',worker_id=%s,
                    started_at=now(),heartbeat_at=now() WHERE task_id=%s""", (worker_id, row[0]))
            return row

    def heartbeat(self, session, worker_id):
        # Use the advisory-lock connection itself so a lost session stops claims.
        session.execute("""UPDATE task_dispatch SET heartbeat_at=now()
            WHERE namespace=%s AND worker_id=%s AND status='running'""", (self.namespace, worker_id))

    def finish(self, task_id, worker_id, *, terminated, reason):
        with self.store._connection() as conn:
            conn.execute("""UPDATE task_dispatch SET status=%s,reason=%s,finished_at=now()
                WHERE task_id=%s AND namespace=%s AND worker_id=%s AND status='running'""",
                         ('terminated' if terminated else 'unknown', reason, task_id, self.namespace, worker_id))
