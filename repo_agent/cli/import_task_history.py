"""Import local task records and evidence without modifying their source files."""
import argparse
import hashlib
import os
from datetime import datetime
from pathlib import Path

from repo_agent.core.config import read_dotenv
from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
from repo_agent.backend.schemas.task_contract import load_json, validate_record
from repo_agent.backend.repositories.task_history import TaskHistory, REGISTRY


def insert_unchanged(conn, table, key_column, key, columns, values):
    # Table and column names are internal constants, never external input.
    names = [key_column, *columns]
    conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join(['%s'] * len(names))}) ON CONFLICT DO NOTHING",
                 (key, *values))
    saved = conn.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {key_column}=%s", (key,)).fetchone()
    expected = tuple(v.obj if hasattr(v, 'obj') else v for v in values)
    if tuple(saved) != expected:
        raise ValueError('Historical record conflict: ' + key)


def import_history(store, root):
    root = Path(root).resolve()
    counts = {'requirements': 0, 'runs': 0, 'materials': 0, 'locations': 0,
              'experiment_tasks': 0, 'experiment_rounds': 0}
    runs = root / 'runs'
    with store._connection() as conn:
        # Import full ordinary history, including legacy task directories.
        for path in sorted(runs.rglob('task.json')):
            if path.resolve() != path:
                raise ValueError('Linked historical task: ' + str(path))
            value = load_json(path.read_bytes())
            if value.get('sourceFormat') == 'ordinary_v2':
                insert_unchanged(conn, 'tasks', 'task_key', value['taskKey'],
                                 ['project_id', 'record'], [value['projectId'], store._json(value)])
                counts['requirements'] += 1
            elif value.get('recordType') == 'actual':
                task_id = value['taskId']
                validate_record(value, task_id)
                metadata_path = path.parent / 'round.json'
                metadata = load_json(metadata_path.read_bytes()) if metadata_path.exists() else {}
                key = metadata.get('taskKey', 'legacy-' + task_id)
                parent_path = path.parent.parent / 'task.json'
                requirement = load_json(parent_path.read_bytes()) if metadata.get('taskKey') else None
                if requirement is None:
                    requirement = {'sourceFormat': 'legacy_task', 'taskKey': key,
                                   'projectId': value['contract']['projectId'], 'contract': value['contract'],
                                   'createdAt': value['createdAt'], 'title': value['contract']['current']}
                insert_unchanged(conn, 'tasks', 'task_key', key, ['project_id', 'record'],
                                 [requirement['projectId'], store._json(requirement)])
                # Empty metadata remains empty. The legacy ID is only a DB lookup key.
                insert_unchanged(conn, 'task_runs', 'task_id', task_id,
                                 ['task_key', 'round_id', 'record', 'metadata', 'created_at'],
                                 [key, metadata.get('round_id', task_id), store._json(value), store._json(metadata),
                                  datetime.fromisoformat(value['createdAt'].replace('Z', '+00:00'))])
                conn.execute('UPDATE task_runs SET source_directory=%s WHERE task_id=%s AND source_directory IS NULL',
                             (str(path.parent), task_id))
                counts['runs'] += 1
        extensions = {'.json', '.jsonl', '.md', '.txt', '.patch', '.diff', '.xml', '.lock'}
        candidates = list(runs.rglob('*'))
        experiments = root / 'experiments'
        candidates.extend(experiments.glob('*.json'))
        candidates.extend(experiments.glob('*/inputs/requirements/**/*'))
        for path in sorted(set(candidates)):
            if not path.is_file() or path.suffix not in extensions:
                continue
            if any(part in {'.git', '.venv', 'venv', 'node_modules', '__pycache__'} for part in path.parts):
                continue
            if path.resolve() != path:
                raise ValueError('Linked historical material: ' + str(path))
            raw = path.read_bytes()
            insert_unchanged(conn, 'historical_materials', 'path', path.relative_to(root).as_posix(),
                             ['sha256', 'content'], [hashlib.sha256(raw).hexdigest(), raw])
            counts['materials'] += 1
        for pattern, kind in [('*/workspaces/*', 'workspace'), ('*/inputs/*', 'input'), ('tasks/*/workspaces/*', 'workspace')]:
            for path in sorted(experiments.glob(pattern)):
                if path.is_dir():
                    insert_unchanged(conn, 'experiment_locations', 'path', path.relative_to(root).as_posix(),
                                     ['kind'], [kind])
                    counts['locations'] += 1
        history = TaskHistory(root)
        for project, (key, _, _) in REGISTRY.items():
            if not (runs / key).is_dir():
                continue
            task = history.task(project, key)
            insert_unchanged(conn, 'experiment_tasks', 'task_key', key,
                             ['project_id', 'record'], [project, store._json(task)])
            counts['experiment_tasks'] += 1
            for view in history.rounds(project, key)['rounds']:
                identifier = view['roundId']
                # This table is a derived page view. Source bytes above must
                # still match before a newer reader may rebuild its projection.
                conn.execute('''INSERT INTO experiment_rounds VALUES (%s,%s,%s)
                             ON CONFLICT(task_key,round_id) DO UPDATE SET record=EXCLUDED.record''',
                             (key, identifier, store._json(view)))
                counts['experiment_rounds'] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    url = os.environ.get('DATABASE_URL') or read_dotenv().get('DATABASE_URL')
    if not url:
        parser.error('DATABASE_URL is required')
    root = args.root.resolve()
    store = DatabaseTaskStore(root / 'runs/tasks', root / 'runs', root / 'experiments', url)
    print(import_history(store, root))


if __name__ == '__main__':
    main()
