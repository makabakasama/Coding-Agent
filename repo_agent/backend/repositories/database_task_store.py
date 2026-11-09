"""PostgreSQL ordinary tasks with recoverable JSON materialization."""
from datetime import datetime
import uuid
from contextlib import contextmanager

from repo_agent.backend.repositories.project_task_store import ProjectTaskStore
from repo_agent.infrastructure.round_layout import new_round_id
from repo_agent.backend.schemas.task_contract import TASK_ID, TaskError, validate_record
from repo_agent.infrastructure.file_io import write_json


class DatabaseTaskStore(ProjectTaskStore):
    def __init__(self, legacy_root, runs_root, workspace_root, database_url):
        super().__init__(legacy_root, runs_root, workspace_root)
        import psycopg
        from psycopg.types.json import Jsonb
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)
        self._database_error = psycopg.Error
        self._json = Jsonb
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
        with self._connection() as conn:
            return conn.execute("""SELECT r.record,r.metadata,t.record,r.source_directory FROM task_runs r
                JOIN tasks t USING(task_key) WHERE r.task_id=%s""", (task_id,)).fetchone()

    def _materialize(self, row):
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

    def create(self, build_record, *, requirement=None):
        task_id = uuid.uuid4().hex
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
        with self._connection() as conn:
            row = conn.execute('SELECT record FROM tasks WHERE project_id=%s AND task_key=%s',
                               (project, key)).fetchone()
        return (self.runs_root / key, row[0]) if row else super().requirement(project, key)

    def requirements(self, project):
        records = {r['taskKey']: r for r in super().requirements(project)}
        with self._connection() as conn:
            for row in conn.execute('SELECT record FROM tasks WHERE project_id=%s', (project,)):
                records[row[0]['taskKey']] = row[0]
        return list(records.values())

    def latest(self):
        records = {r['taskId']: r for r in super().latest()}
        with self._connection() as conn:
            for row in conn.execute('SELECT record FROM task_runs ORDER BY created_at DESC,task_id DESC LIMIT 50'):
                records[row[0]['taskId']] = row[0]
        return sorted(records.values(), key=lambda r: (datetime.fromisoformat(r['createdAt'].replace('Z', '+00:00')),
                                                       r['taskId']), reverse=True)[:50]

    def rounds(self, project, key):
        # Regenerate any missing JSON before the existing round projection reads it.
        with self._connection() as conn:
            rows = conn.execute("""SELECT r.record,r.metadata,t.record,r.source_directory FROM task_runs r
                JOIN tasks t USING(task_key) WHERE t.project_id=%s AND t.task_key=%s""", (project, key)).fetchall()
        for row in rows:
            self._materialize(row)
        return super().rounds(project, key)
