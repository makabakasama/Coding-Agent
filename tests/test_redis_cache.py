import time
import unittest
from unittest.mock import Mock
import redis

from repo_agent.infrastructure.redis_cache import RedisCache


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False
        self.reads = 0

    def get(self, key):
        self.reads += 1
        if self.fail:
            raise redis.ConnectionError('private connection details')
        value = self.values.get(key)
        if value and value[1] > time.monotonic():
            return value[0]
        return None

    def set(self, key, value, *, ex=None, nx=False):
        if self.fail:
            raise redis.ConnectionError('private connection details')
        if nx and key in self.values and self.values[key][1] > time.monotonic():
            return False
        expires = time.monotonic() + ex if ex is not None else float('inf')
        self.values[key] = (value.encode() if isinstance(value,str) else value, expires)
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key,None)


class RedisCacheTest(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.cache = RedisCache(namespace='test',client=self.redis)

    def test_second_process_cache_hit_avoids_loader(self):
        loader = Mock(return_value={'taskId':'test'})
        self.assertEqual({'taskId':'test'},self.cache.json('run',loader))
        other = RedisCache(namespace='test',client=self.redis)
        self.assertEqual({'taskId':'test'},other.json('run',loader))
        loader.assert_called_once()

    def test_expiration_and_invalidation_reload_database(self):
        loader = Mock(return_value={'status':'queued'})
        self.cache.json('dispatch',loader,ttl=1)
        loader.return_value = {'status':'running'}
        self.cache.delete('dispatch')
        self.assertEqual({'status':'running'},self.cache.json('dispatch',loader,ttl=1))
        raw,_ = self.redis.values['test:dispatch']
        self.redis.values['test:dispatch'] = (raw,0)
        self.cache.json('dispatch',loader,ttl=1)
        self.assertEqual(3,loader.call_count)

    def test_corrupt_cache_and_empty_binary_content(self):
        self.redis.set('test:record',b'broken-json',ex=300)
        self.assertEqual({'ok':True},self.cache.json('record',lambda:{'ok':True}))
        loader = Mock(return_value=b'')
        self.assertEqual(b'',self.cache.binary('log',loader))
        self.assertEqual(b'',self.cache.binary('log',loader))
        loader.assert_called_once()

    def test_outage_falls_back_and_avoids_repeated_network_waits(self):
        self.redis.fail = True
        loader = Mock(return_value={'ok':True})
        with self.assertLogs('repo_agent.infrastructure.redis_cache',level='WARNING') as logs:
            self.cache.json('run',loader)
            self.cache.json('run',loader)
        self.assertEqual(1,self.redis.reads)
        self.assertEqual(2,loader.call_count)
        self.assertNotIn('private connection details','\n'.join(logs.output))

    def test_absent_record_is_not_negatively_cached(self):
        loader = Mock(return_value=None)
        self.assertIsNone(self.cache.json('new',loader))
        loader.return_value = {'created':True}
        self.assertEqual({'created':True},self.cache.json('new',loader))

    def test_late_snapshot_fill_cannot_override_invalidated_generation(self):
        def overlapping_read():
            self.cache.invalidate_generation('lists')
            return ['old']
        self.assertEqual(['old'], self.cache.versioned_json('tasks', overlapping_read, generation='lists'))
        loader = Mock(return_value=['new'])
        self.assertEqual(['new'], self.cache.versioned_json('tasks', loader, generation='lists'))
        self.assertEqual(['new'], self.cache.versioned_json('tasks', loader, generation='lists'))
        loader.assert_called_once()

    def test_lost_generation_key_does_not_resurrect_an_older_snapshot(self):
        self.cache.versioned_json('tasks', lambda: ['old'], generation='lists')
        self.cache.delete('lists:revision')
        self.assertEqual(['new'], self.cache.versioned_json('tasks', lambda: ['new'], generation='lists'))

    def test_versioned_cache_supports_different_snapshot_lifetimes(self):
        self.cache.versioned_json('tasks', lambda: [], generation='lists', ttl=lambda value: 30)
        snapshot = next(value for key, value in self.redis.values.items() if ':tasks:' in key)
        self.assertGreater(snapshot[1] - time.monotonic(), 29)

    def test_versioned_cache_failure_returns_live_data(self):
        self.redis.fail = True
        loader = Mock(return_value=['live'])
        with self.assertLogs('repo_agent.infrastructure.redis_cache', level='WARNING'):
            self.assertEqual(['live'], self.cache.versioned_json('tasks', loader, generation='lists'))
        loader.assert_called_once()
