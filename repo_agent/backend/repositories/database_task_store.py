"""PostgreSQL ordinary tasks with recoverable JSON materialization."""
from datetime import datetime
import uuid
import hashlib
from contextlib import contextmanager

from repo_agent.backend.repositories.project_task_store import ProjectTaskStore
from repo_agent.infrastructure.round_layout import new_round_id
from repo_agent.backend.schemas.task_contract import TASK_ID, TaskError, validate_record
from repo_agent.infrastructure.file_io import write_json


class DatabaseTaskStore(ProjectTaskStore):
    def __init__(self, legacy_root, runs_root, workspace_root, database_url, *, redis_url=None, cache=None):
        super().__init__(legacy_root, runs_root, workspace_root)
        import psycopg
        from psycopg.types.json import Jsonb
        from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests
        self.pool = ConnectionPool(database_url, min_size=0, max_size=6, timeout=5,
                                   kwargs={'connect_timeout': 5}, reset=self._reset_connection,
                                   open=False)
        self._connect = self.pool.connection
        self._database_error = (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests)
        self._json = Jsonb
        self.cache = cache
        if self.cache is None and redis_url:
            from repo_agent.infrastructure.redis_cache import RedisCache
            namespace = hashlib.sha256((database_url + str(self.root) + str(self.runs_root)).encode()).hexdigest()
            self.cache = RedisCache(redis_url, namespace='coding-agent:v1:' + namespace)
        try:
            self.pool.open()
            with self._connection() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                    task_key text PRIMARY KEY, project_id text NOT NULL,
                    record jsonb NOT NULL)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS task_runs (
                    task_id text PRIMARY KEY, task_key text NOT NULL REFERENCES tasks(task_key),
                    round_id text NOT NULL, created_at timestamptz NOT NULL,
                    record jsonb NOT NULL, metadata jsonb NOT NULL,
                    UNIQUE(task_key, round_id))""")
                conn.execute('ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS source_directory text')
                conn.execute('''CREATE TABLE IF NOT EXISTS task_trash (
                    task_id text PRIMARY KEY, deleted_at timestamptz NOT NULL DEFAULT now(),
                    dispatch jsonb)''')
                conn.execute("""CREATE TABLE IF NOT EXISTS historical_materials (
                    path text PRIMARY KEY, sha256 text NOT NULL, content bytea NOT NULL,
                    imported_at timestamptz NOT NULL DEFAULT now())""")
                conn.execute("""CREATE TABLE IF NOT EXISTS experiment_locations (
                    path text PRIMARY KEY, kind text NOT NULL)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS experiment_tasks (
                    task_key text PRIMARY KEY, project_id text NOT NULL, record jsonb NOT NULL)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS experiment_rounds (
                    task_key text NOT NULL REFERENCES experiment_tasks(task_key),
                    round_id text NOT NULL, record jsonb NOT NULL, PRIMARY KEY(task_key,round_id))""")
        except Exception:
            self.close()
            raise

    @staticmethod
    def _reset_connection(connection):
        # The Worker holds an autocommit session for its advisory pool lock.
        # Ordinary request transactions must always return to manual commit.
        connection.autocommit = False

    def close(self):
        self.pool.close()

    @contextmanager
    def _connection(self):
        try:
            with self._connect() as conn:
                yield conn
        except self._database_error as exc:
            raise TaskError(503, 'DATABASE_UNAVAILABLE', '任务数据库操作失败，请检查连接配置') from exc

    def _run(self, task_id):
        if not TASK_ID.fullmatch(task_id):
            raise TaskError(400, 'INVALID_TASK_ID', '任务编号格式不正确')
        self.assert_not_purged(task_id)
        def load():
            with self._connection() as conn:
                return conn.execute("""SELECT r.record,r.metadata,t.record,r.source_directory FROM task_runs r
                    JOIN tasks t USING(task_key) WHERE r.task_id=%s""", (task_id,)).fetchone()
        return self.cached_json('run:' + task_id, load)

    def cached_json(self, key, loader, *, ttl=300):
        return self.cache.json(key, loader, ttl=ttl) if self.cache else loader()

    def invalidate_run(self, task_id):
        if self.cache:
            self.cache.delete('run:' + task_id)
        self.invalidate_lists()

    def invalidate_lists(self):
        if self.cache:
            self.cache.invalidate_generation('task-lists')

    def deleted_ids(self):
        with self._connection() as conn:
            return {row[0] for row in conn.execute('SELECT task_id FROM task_trash')}

    def is_deleted(self, task_id):
        with self._connection() as conn:
            deleted = conn.execute('SELECT 1 FROM task_trash WHERE task_id=%s', (task_id,)).fetchone()
        return deleted is not None

    def set_deleted(self, task_id, deleted):
        self.get(task_id)
        with self._connection() as conn:
            # Enqueue takes this same lock. Claim and deletion serialize on dispatch.
            conn.execute('SELECT task_id FROM task_runs WHERE task_id=%s FOR UPDATE', (task_id,)).fetchone()
            dispatch = conn.execute('SELECT status,to_jsonb(d) FROM task_dispatch d WHERE task_id=%s FOR UPDATE',
                                    (task_id,)).fetchone()
            if deleted:
                if dispatch and dispatch[0] in ('running', 'unknown'):
                    raise TaskError(409, 'TASK_BUSY', '任务仍在执行或待核实，暂不能删除')
                conn.execute('INSERT INTO task_trash(task_id,dispatch) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                             (task_id, self._json(dispatch[1]) if dispatch else None))
                # Restoring a queued task requires a fresh explicit start.
                conn.execute("DELETE FROM task_dispatch WHERE task_id=%s AND status='queued'", (task_id,))
            else:
                conn.execute('DELETE FROM task_trash WHERE task_id=%s', (task_id,))
        if self.cache:
            self.cache.delete('dispatch:' + task_id)
        self.invalidate_lists()

    def purge_run(self, task_id, cleanup, *, relative_directory=None, relative_requirement=None):
        requirement = None
        with self._connection() as conn:
            row = conn.execute('SELECT task_key FROM task_runs WHERE task_id=%s FOR UPDATE', (task_id,)).fetchone()
            trash = conn.execute('SELECT 1 FROM task_trash WHERE task_id=%s FOR UPDATE', (task_id,)).fetchone()
            dispatch = conn.execute('SELECT status FROM task_dispatch WHERE task_id=%s FOR UPDATE', (task_id,)).fetchone()
            if dispatch and dispatch[0] in ('running', 'unknown'):
                raise TaskError(409, 'TASK_BUSY', '执行中或待核实的任务不能彻底删除')
            if not trash and row:
                raise TaskError(409, 'TRASH_REQUIRED', '请先将本轮移入回收站')
            if not trash and not self.purge_path(task_id).exists():
                raise TaskError(404, 'TASK_NOT_FOUND', '任务不存在')
            last = False
            if row:
                project = conn.execute('SELECT project_id FROM tasks WHERE task_key=%s FOR UPDATE', row).fetchone()
                requirement = (project[0], row[0])
                last = not conn.execute('SELECT 1 FROM task_runs WHERE task_key=%s AND task_id<>%s LIMIT 1',
                                        (row[0], task_id)).fetchone()
            cleanup(last)
            if relative_directory:
                prefix = relative_directory + '/'
                conn.execute('DELETE FROM historical_materials WHERE left(path,length(%s))=%s', (prefix, prefix))
                conn.execute('DELETE FROM experiment_locations WHERE path=%s OR left(path,length(%s))=%s',
                             (relative_directory, prefix, prefix))
            conn.execute('DELETE FROM task_dispatch WHERE task_id=%s', (task_id,))
            conn.execute('DELETE FROM task_runs WHERE task_id=%s', (task_id,))
            conn.execute('DELETE FROM task_trash WHERE task_id=%s', (task_id,))
            if row and last:
                conn.execute('DELETE FROM tasks WHERE task_key=%s', row)
                if relative_requirement:
                    conn.execute('DELETE FROM historical_materials WHERE path=%s', (relative_requirement,))
        self.invalidate_run(task_id)
        if self.cache:
            keys = ['dispatch:' + task_id]
            if requirement:
                keys.append('requirement:' + requirement[0] + ':' + requirement[1])
            self.cache.delete(*keys)

    def _materialize(self, row):
        from pathlib import Path
        from repo_agent.infrastructure.file_io import exclusive
        task_id = row[0]['taskId']
        self.assert_not_purged(task_id)
        if len(row) > 3 and row[3]:
            return Path(row[3])
        parent = self.runs_root / row[2]['taskKey']
        directory = parent / ('run_' + row[1]['round_id'])
        paths = (parent / 'task.json', directory / 'round.json', directory / 'task.json')
        if all(path.exists() and path.resolve() == path for path in paths):
            return directory
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive(self.root / ('material-' + task_id + '.lock')) as acquired:
            if not acquired:
                raise TaskError(503, 'MATERIAL_BUSY', '本轮材料正在更新，请稍后读取')
            self.assert_not_purged(task_id)
            return self._materialize_locked(row)

    def _materialize_locked(self, row):
        record, metadata, requirement = row[:3]
        if len(row) > 3 and row[3]:
            from pathlib import Path
            return Path(row[3])
        parent = self.runs_root / requirement['taskKey']
        directory = parent / ('run_' + metadata['round_id'])
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            directory.mkdir(parents=True, exist_ok=True)
            for path, value in ((parent / 'task.json', requirement),
                                (directory / 'round.json', metadata), (directory / 'task.json', record)):
                if path.resolve() != path:
                    raise OSError('Unexpected task material location')
                if not path.exists():
                    temporary = path.with_name(uuid.uuid4().hex + '.json')
                    try:
                        write_json(temporary, value)
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise TaskError(503, 'STORAGE_UNAVAILABLE',
                            '任务已存入数据库，本地材料生成失败，读取该任务可重试生成') from exc
        return directory

    def create(self, build_record, *, requirement=None, task_id=None):
        task_id = task_id or uuid.uuid4().hex
        if not TASK_ID.fullmatch(task_id):
            raise ValueError('Invalid preallocated task id')
        record = build_record(task_id)
        validate_record(record, task_id)
        contract = record['contract']
        if requirement is None:
            key = contract['projectId'] + '-' + uuid.uuid4().hex[:12]
            value = {'sourceFormat': 'ordinary_v2', 'mode': 'ordinary',
                     'projectId': contract['projectId'], 'taskKey': key,
                     'createdAt': record['createdAt'], 'title': contract['current'], 'contract': contract}
        else:
            _, value = self.requirement(*requirement)
            key = value['taskKey']
        metadata = {'round_id': new_round_id(), 'entry': 'run', 'experiment': key,
                    'display_timezone': 'UTC+08:00', 'mode': 'ordinary',
                    'projectId': value['projectId'], 'taskKey': key,
                    'runTaskId': task_id, 'sourceFormat': 'ordinary_v2'}
        with self._connection() as conn:
            conn.execute("INSERT INTO tasks VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                         (key, value['projectId'], self._json(value)))
            conn.execute("INSERT INTO task_runs(task_id,task_key,round_id,created_at,record,metadata) VALUES (%s,%s,%s,%s,%s,%s)",
                         (task_id, key, metadata['round_id'], record['createdAt'],
                          self._json(record), self._json(metadata)))
        self.invalidate_run(task_id)
        try:
            self._materialize((record, metadata, value))
        except TaskError as exc:
            raise TaskError(exc.status, exc.code,
                            f'任务 {task_id} 已存入数据库，本地材料生成失败，请读取该任务恢复材料') from exc
        return record

    def get(self, task_id):
        row = self._run(task_id)
        if row is None:
            return super().get(task_id)
        validate_record(row[0], task_id)
        self._materialize(row)
        return row[0]

    def directory(self, task_id):
        row = self._run(task_id)
        return self._materialize(row) if row else super().directory(task_id)

    def requirement(self, project, key):
        def load():
            with self._connection() as conn:
                return conn.execute('SELECT record FROM tasks WHERE project_id=%s AND task_key=%s',
                                    (project, key)).fetchone()
        row = self.cached_json('requirement:' + project + ':' + key, load)
        return (self.runs_root / key, row[0]) if row else super().requirement(project, key)

    def requirements(self, project):
        records = {r['taskKey']: r for r in super().requirements(project)}
        with self._connection() as conn:
            for row in conn.execute('SELECT record FROM tasks WHERE project_id=%s', (project,)):
                records[row[0]['taskKey']] = row[0]
        return list(records.values())

    def latest(self, *, deleted=False):
        records = {r['taskId']: r for r in super().latest(deleted=deleted)}
        with self._connection() as conn:
            for row in conn.execute('''SELECT r.record FROM task_runs r
                    LEFT JOIN task_trash trash USING(task_id) WHERE (trash.task_id IS NOT NULL)=%s
                    ORDER BY r.created_at DESC,r.task_id DESC LIMIT 50''', (deleted,)):
                if not self.purge_path(row[0]['taskId']).exists():
                    records[row[0]['taskId']] = row[0]
        return sorted(records.values(), key=lambda r: (datetime.fromisoformat(r['createdAt'].replace('Z', '+00:00')),
                                                       r['taskId']), reverse=True)[:50]

    def rounds(self, project, key):
        # Regenerate any missing JSON before the existing round projection reads it.
        with self._connection() as conn:
            rows = conn.execute("""SELECT r.record,r.metadata,t.record,r.source_directory FROM task_runs r
                JOIN tasks t USING(task_key) WHERE t.project_id=%s AND t.task_key=%s""", (project, key)).fetchall()
        for row in rows:
            if not self.purge_path(row[0]['taskId']).exists():
                self._materialize(row)
        return super().rounds(project, key)

    def round_counts(self, project):
        with self._connection() as conn:
            rows = conn.execute("""SELECT t.task_key,count(r.task_id) FROM tasks t
                LEFT JOIN task_runs r ON r.task_key=t.task_key AND NOT EXISTS (
                    SELECT 1 FROM task_trash trash WHERE trash.task_id=r.task_id)
                WHERE t.project_id=%s GROUP BY t.task_key""", (project,)).fetchall()
        return dict(rows)

    def hidden_requirements(self, project):
        hidden = super().hidden_requirements(project)
        with self._connection() as conn:
            rows = conn.execute('''SELECT t.task_key FROM tasks t
                JOIN task_runs r USING(task_key) LEFT JOIN task_trash trash USING(task_id)
                WHERE t.project_id=%s GROUP BY t.task_key
                HAVING count(trash.task_id)=count(r.task_id)''', (project,)).fetchall()
        return hidden | {row[0] for row in rows}
