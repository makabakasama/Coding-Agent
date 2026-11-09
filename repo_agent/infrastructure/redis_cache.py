"""Bounded Redis read-through cache. Storage and scheduling remain authoritative."""
import json
import logging
import time
import uuid


logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(self, url=None, *, namespace, client=None):
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        self.client = client if client is not None else redis.Redis.from_url(
            url, socket_connect_timeout=.15, socket_timeout=.15,
            retry=Retry(NoBackoff(), 0), health_check_interval=30)
        self.namespace = namespace
        self.error_type = redis.RedisError
        self.retry_at = 0

    def _call(self, method, *args, **kwargs):
        if time.monotonic() < self.retry_at:
            return None
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except self.error_type:
            self.retry_at = time.monotonic() + 5
            logger.warning('Redis cache unavailable; using PostgreSQL for reads')
            return None

    def key(self, key):
        return self.namespace + ':' + key

    def json(self, key, loader, *, ttl=300):
        raw = self._call('get', self.key(key))
        if raw is not None:
            try:
                return json.loads(raw)
            except (ValueError, TypeError, UnicodeError):
                self.delete(key)
        value = loader()
        if value is not None:
            lifetime = ttl(value) if callable(ttl) else ttl
            self._call('set', self.key(key), json.dumps(value, ensure_ascii=False), ex=lifetime)
        return value

    def versioned_json(self, key, loader, *, generation, ttl=30):
        revision_key = self.key(generation + ':revision')
        revision = self._call('get', revision_key)
        if revision is None:
            self._call('set', revision_key, uuid.uuid4().hex, nx=True)
            revision = self._call('get', revision_key)
        if revision is None:
            return loader()
        # An overlapping write rotates the revision. A late reader can only
        # populate its old version and cannot overwrite the new snapshot.
        return self.json(key + ':' + revision.hex(), loader, ttl=ttl)

    def invalidate_generation(self, generation):
        self._call('set', self.key(generation + ':revision'), uuid.uuid4().hex)

    def binary(self, key, loader, *, ttl=300):
        raw = self._call('get', self.key(key))
        if raw is not None:
            return bytes(raw)
        value = loader()
        if value is not None:
            self._call('set', self.key(key), value, ex=ttl)
        return value

    def delete(self, *keys):
        if keys:
            self._call('delete', *(self.key(key) for key in keys))
