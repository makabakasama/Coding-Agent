"""Offline image-provider regressions; Docker and the evaluator are stubbed.

Covers source verification plus the content-keyed derived-image cache. No
Docker daemon or model call is made.
"""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from repo_agent.evaluation.container_lifecycle import MEMORY_BYTES
from repo_agent.evaluation.image_provider import (
    CACHE_NAMESPACE, RESTORE_RECIPE_VERSION, cache_tag, probe_image,
    resolve_verified_image, restore_dockerfile)


class ImageProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.commit, self.tree = 'a' * 40, 'b' * 40
        self.image = SimpleNamespace(id='sha256:' + 'c' * 64, attrs={})
        self.fixed = SimpleNamespace(id='sha256:' + 'd' * 64, attrs={}, tag=Mock(return_value=True))
        self.row = {'image': 'official:latest', 'base_commit': self.commit,
                    'test_patch': 'frozen R', 'eval_script': 'frozen command'}
        self.client = Mock()
        self.client.images.get.return_value = self.image
        self.missing = type('ImageNotFound', (Exception,), {})
        modules = patch.dict('sys.modules', {'docker.errors': SimpleNamespace(
            ImageNotFound=self.missing,
            NotFound=type('NotFound', (Exception,), {}))})
        modules.start()
        self.addCleanup(modules.stop)

    def clean(self, **changes):
        return {'head': self.commit, 'tree': self.tree, 'status': '', **changes}

    def cold_client(self):
        """A client whose tag lookup misses, so prepare takes the full path."""
        def get(reference):
            if isinstance(reference, str) and reference.startswith(CACHE_NAMESPACE + ':'):
                raise self.missing('no cached image')
            return self.fixed if reference == self.fixed.id else self.image

        self.client.images.get.side_effect = get
        return self.client

    def test_matching_image_is_checked_without_rebuild_and_only_image_changes(self):
        with patch('repo_agent.evaluation.image_provider.probe_image',
                   return_value=self.clean()) as probe:
            row = resolve_verified_image(self.cold_client(), self.row, self.commit,
                                         self.tree, self.root)
        self.assertEqual(2, probe.call_count)
        self.client.api.build.assert_not_called()
        self.assertEqual({**self.row, 'image': self.image.id}, row)
        self.assertEqual('official:latest', self.row['image'])

    def test_wrong_source_is_restored_and_rechecked(self):
        client = self.cold_client()
        client.api.build.return_value = [{'aux': {'ID': self.fixed.id}}]
        with patch('repo_agent.evaluation.image_provider.probe_image',
                   side_effect=[self.clean(head='e' * 40), self.clean()]):
            row = resolve_verified_image(client, self.row, self.commit, self.tree, self.root)
        self.assertEqual(self.fixed.id, row['image'])
        dockerfile = (self.root / 'image-override/Dockerfile').read_text()
        self.assertIn(self.image.id, dockerfile)
        self.assertIn('reset --hard ' + self.commit, dockerfile)
        self.assertEqual('none', client.api.build.call_args.kwargs['network_mode'])
        self.assertTrue(client.api.build.call_args.kwargs['rm'])
        self.assertTrue(client.api.build.call_args.kwargs['forcerm'])
        self.assertEqual({'memory': MEMORY_BYTES, 'memswap': MEMORY_BYTES},
                         client.api.build.call_args.kwargs['container_limits'])
        verification = json.loads((self.root / 'source-verification.json').read_bytes())
        self.assertTrue(verification['sourceRestored'])
        self.assertFalse(verification['cache']['hit'])

    def test_wrong_tree_after_restore_blocks_scoring(self):
        client = self.cold_client()
        client.api.build.return_value = [{'aux': {'ID': self.fixed.id}}]
        with patch('repo_agent.evaluation.image_provider.probe_image',
                   side_effect=[self.clean(status=' M app.py'), self.clean(tree='e' * 40)]):
            with self.assertRaisesRegex(ValueError, 'fixed clean source'):
                resolve_verified_image(client, self.row, self.commit, self.tree, self.root)
        self.assertFalse((self.root / 'source-verification.json').exists())

    def test_failed_build_keeps_output_and_does_not_mark_verified(self):
        client = self.cold_client()
        client.api.build.return_value = [{'stream': 'building'}, {'error': 'failed'}]
        with patch('repo_agent.evaluation.image_provider.probe_image',
                   return_value=self.clean(head='e' * 40)):
            with self.assertRaisesRegex(ValueError, 'build failed'):
                resolve_verified_image(client, self.row, self.commit, self.tree, self.root)
        self.assertIn('failed', (self.root / 'image-build.jsonl').read_text())
        self.assertFalse((self.root / 'source-verification.json').exists())

    def test_probe_failure_stops_and_removes_container_after_metadata(self):
        container = self.client.containers.create.return_value
        container.id = 'offline-probe'
        container.exec_run.return_value = SimpleNamespace(exit_code=1, output=b'bad source')
        with self.assertRaises(ValueError):
            probe_image(self.client, self.image, self.root, 'failed')
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(force=True, v=False)
        for key, value in (
                ('network_disabled', True), ('mem_limit', MEMORY_BYTES),
                ('memswap_limit', MEMORY_BYTES), ('nano_cpus', 1_000_000_000),
                ('pids_limit', 256)):
            self.assertEqual(value, self.client.containers.create.call_args.kwargs[key])

    def test_probe_record_failure_also_removes_container(self):
        container = self.client.containers.create.return_value
        container.id = 'offline-probe'
        with patch('repo_agent.evaluation.image_provider.save',
                   side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                probe_image(self.client, self.image, self.root, 'failed-save')
        container.remove.assert_called_once_with(force=True, v=False)

    def test_dockerfile_rejects_injected_identity(self):
        with self.assertRaises(ValueError):
            restore_dockerfile(self.image.id, self.commit + '\nRUN false')

    def test_cache_tag_is_content_addressed(self):
        first = cache_tag(self.image.id, self.commit, self.tree)
        self.assertEqual(first, cache_tag(self.image.id, self.commit, self.tree))
        self.assertTrue(first.startswith(CACHE_NAMESPACE + ':'))
        self.assertNotEqual(first, cache_tag(self.fixed.id, self.commit, self.tree))
        self.assertNotEqual(first, cache_tag(self.image.id, 'e' * 40, self.tree))
        self.assertNotEqual(first, cache_tag(self.image.id, self.commit, 'f' * 40))
        material = json.dumps({'image': self.image.id, 'baseCommit': self.commit,
                               'tree': self.tree, 'recipe': RESTORE_RECIPE_VERSION},
                              sort_keys=True, separators=(',', ':'))
        self.assertEqual(first.split(':')[1], hashlib.sha256(material.encode()).hexdigest())

    def test_cache_hit_reuses_verified_image_without_build(self):
        tag = cache_tag(self.image.id, self.commit, self.tree)
        self.client.images.get.side_effect = lambda ref: self.fixed if ref == tag else self.image
        with patch('repo_agent.evaluation.image_provider.probe_image',
                   return_value=self.clean()) as probe:
            row = resolve_verified_image(self.client, self.row, self.commit, self.tree, self.root)
        self.assertEqual(self.fixed.id, row['image'])
        probe.assert_called_once()
        self.assertEqual('cache', probe.call_args.args[3])
        self.client.api.build.assert_not_called()
        verification = json.loads((self.root / 'source-verification.json').read_bytes())
        self.assertTrue(verification['cache']['hit'])
        self.assertEqual('verified_reuse', verification['cache']['reason'])
        self.assertEqual('reused', json.loads(
            (self.root / 'image-cache.json').read_bytes())['status'])

    def test_stale_cache_degrades_to_full_build(self):
        tag = cache_tag(self.image.id, self.commit, self.tree)
        self.client.images.get.side_effect = lambda ref: self.fixed if ref in (tag, self.fixed.id) else self.image
        self.client.api.build.return_value = [{'aux': {'ID': self.fixed.id}}]
        with patch('repo_agent.evaluation.image_provider.probe_image',
                   side_effect=[self.clean(head='e' * 40), self.clean(head='e' * 40), self.clean()]) as probe:
            row = resolve_verified_image(self.client, self.row, self.commit, self.tree, self.root)
        self.assertEqual(self.fixed.id, row['image'])
        self.client.api.build.assert_called_once()
        verification = json.loads((self.root / 'source-verification.json').read_bytes())
        self.assertFalse(verification['cache']['hit'])
        self.assertEqual(['cache', 'original', 'effective'], [call.args[-1] for call in probe.call_args_list])
        self.assertEqual('tagged', json.loads(
            (self.root / 'image-cache.json').read_bytes())['status'])

    def test_cache_probe_error_degrades_without_build_when_source_clean(self):
        tag = cache_tag(self.image.id, self.commit, self.tree)
        self.client.images.get.side_effect = lambda ref: self.fixed if ref == tag else self.image

        def probe(client, image, evidence, name):
            if name == 'cache':
                raise OSError('daemon unreachable')
            return self.clean()

        with patch('repo_agent.evaluation.image_provider.probe_image', side_effect=probe):
            row = resolve_verified_image(self.client, self.row, self.commit, self.tree, self.root)
        self.assertEqual(self.image.id, row['image'])
        self.client.api.build.assert_not_called()
        verification = json.loads((self.root / 'source-verification.json').read_bytes())
        self.assertFalse(verification['cache']['hit'])
        self.assertEqual('probe_failed', verification['cache']['reason'])
        self.assertEqual('OSError', verification['cache']['errorType'])


if __name__ == '__main__':
    unittest.main()
