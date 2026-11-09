import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from repo_agent.backend.repositories.database_task_history import DatabaseTaskHistory


class SnapshotHistory(DatabaseTaskHistory):
    def __init__(self, root, materials):
        super().__init__(root, None)
        self.materials = materials

    def _stored_bytes(self, path):
        return self.materials.get(path.relative_to(self.root).as_posix())

    def _registration(self, project, task=None):
        return None, SimpleNamespace(name='sample', path_migrations=None), self.root / 'runs/sample'


class DatabaseHistoryTest(unittest.TestCase):
    def test_legacy_materials_are_readable_from_database_without_local_copies(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            identifier = '20261024-143821_4e540f1e'
            directory = root / 'runs/sample' / ('legacy_' + identifier)
            directory.mkdir(parents=True)
            metadata = {'round_id': identifier, 'entry': 'legacy', 'experiment': 'sample'}
            materials = {
                (directory / 'round.json').relative_to(root).as_posix(): json.dumps(metadata).encode(),
                (directory / 'repair/metadata.json').relative_to(root).as_posix(): b'{"status":"prepared","api_key":"secret"}',
                (directory / 'repair/baseline-tests.json').relative_to(root).as_posix(): b'{"exit_code":1}',
                (directory / 'repair/agent-console.txt').relative_to(root).as_posix(): b'',
                (directory / 'repair/before-agent.diff').relative_to(root).as_posix(): b'--- before\n+++ after\n',
            }
            history = SnapshotHistory(root, materials)
            view = history.round('sample', 'sample', identifier)
            available = {a['artifactId'] for a in view['artifacts'] if a['available']}
            self.assertIn('legacy-metadata-json', available)
            self.assertIn('legacy-agent-console-txt', available)
            self.assertEqual('unlinked', view['association'])
            self.assertIsNone(view['agentStatus'])
            self.assertIsNone(view['originalRunConclusion'])
            self.assertTrue(any('experiment.json' in w for w in view['warnings']))
            public = history.artifact('sample', 'sample', identifier, 'legacy-metadata-json')['content']
            self.assertIn('[REDACTED]', public)
            self.assertNotIn('secret', public)
            empty = history.artifact('sample', 'sample', identifier, 'legacy-agent-console-txt')
            self.assertEqual('', empty['content'])
