"""Database tests use an explicitly supplied dedicated PostgreSQL test database."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
from repo_agent.backend.repositories.project_task_store import ProjectTaskStore
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.infrastructure.file_io import read_json
from tests.test_tasks import request_contract


def record(task_id):
    contract = request_contract()
    contract['requestedRevision'] = contract.pop('revision')
    contract['baseCommit'] = 'a' * 40
    contract.setdefault('kind', 'fix_bug')
    contract.setdefault('maxSteps', 12)
    return {'schemaVersion': 1, 'taskId': task_id, 'recordType': 'actual',
            'status': 'created', 'createdAt': '2026-11-07T00:00:00Z', 'contract': contract}


class MaterializationTest(unittest.TestCase):
    def test_database_failure_does_not_write_task_files(self):
        import psycopg
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('psycopg.connect', side_effect=psycopg.OperationalError('private connection details')):
                with self.assertRaises(TaskError) as error:
                    DatabaseTaskStore(root / 'data', root / 'runs', root / 'workspaces', 'unused')
            self.assertEqual('DATABASE_UNAVAILABLE', error.exception.code)
            self.assertNotIn('private connection details', error.exception.message)
            self.assertFalse((root / 'runs').exists())

    def test_missing_materials_are_recovered_without_overwriting_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = DatabaseTaskStore.__new__(DatabaseTaskStore)
            ProjectTaskStore.__init__(store, root / 'data', root / 'runs', root / 'workspaces')
            row = (record('a' * 32), {'round_id': '20261107-080000_12345678'}, {'taskKey': 'sample-test'})
            directory = store._materialize(row)
            self.assertEqual(row[0], read_json(directory / 'task.json'))
            (directory / 'task.json').unlink()
            store._materialize(row)
            self.assertEqual(row[0], read_json(directory / 'task.json'))
            (directory / 'task.json').write_text('historical evidence', encoding='utf-8')
            store._materialize(row)
            self.assertEqual('historical evidence', (directory / 'task.json').read_text())


@unittest.skipUnless(os.environ.get('CODING_AGENT_TEST_DATABASE_URL'), 'Dedicated test database not configured')
class PostgreSQLTest(unittest.TestCase):
    def test_create_restart_second_round_and_file_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = (root / 'data', root / 'runs', root / 'workspaces', os.environ['CODING_AGENT_TEST_DATABASE_URL'])
            store = DatabaseTaskStore(*args)
            keys = []
            try:
                first = store.create(record)
                _, requirement = store.requirement('sample', store.round_info(first['taskId'])['taskKey'])
                keys.append(requirement['taskKey'])
                second = store.create(record, requirement=('sample', keys[0]))
                restarted = DatabaseTaskStore(*args)
                self.assertEqual(first, restarted.get(first['taskId']))
                self.assertEqual(2, len(restarted.rounds('sample', keys[0])))
                path = restarted.directory(second['taskId']) / 'task.json'
                path.unlink()
                self.assertEqual(second, restarted.get(second['taskId']))
                self.assertTrue(path.is_file())
                with patch.object(store, '_materialize', side_effect=TaskError(503, 'STORAGE_UNAVAILABLE', 'test')):
                    with self.assertRaises(TaskError):
                        store.create(record, requirement=('sample', keys[0]))
                self.assertEqual(3, len(restarted.rounds('sample', keys[0])))
            finally:
                with store._connection() as conn:
                    for key in keys:
                        conn.execute('DELETE FROM task_runs WHERE task_key=%s', (key,))
                        conn.execute('DELETE FROM tasks WHERE task_key=%s', (key,))
