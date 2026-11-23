"""Shared evaluation container slot and cleanup; importing starts no work."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile


MEMORY_BYTES = 1024 ** 3
CONTAINER_LIMITS = {'mem_limit': MEMORY_BYTES, 'memswap_limit': MEMORY_BYTES,
                    'nano_cpus': 1_000_000_000, 'pids_limit': 256}


def save(path, value):
    """Exclusive JSON write shared by the launcher and the image provider."""
    with path.open('xb') as stream:
        stream.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def runtime_root():
    return Path(os.environ['CODING_AGENT_DOCKER_TEMP']) if os.environ.get('CODING_AGENT_DOCKER_TEMP') else Path(tempfile.gettempdir()) / 'coding-agent-docker'


@contextmanager
def container_slot():
    """One OS-locked slot across check/grader processes, fail closed if busy."""
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'evaluation-container.lock').open('a+b') as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another evaluation owns the container slot; no container was started') from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def require_idle(client):
    # A killed process releases its file lock but may leave a container running.
    # Refuse a new run instead of overlapping it or deleting an unknown workload.
    for container in client.containers.list():
        role = (container.labels or {}).get('coding-agent.role')
        if role in {'developer-check', 'grading-preflight', 'official-grading'} or container.name.startswith('sweb.eval.'):
            raise RuntimeError('An evaluation container is already running; clean it up before starting another')


def remove_container(container, evidence, name):
    """Always attempt removal, even when stop fails. Never prune volumes/images."""
    from docker.errors import NotFound
    record = {'containerId': container.id}
    try:
        try:
            container.stop(timeout=5)
        except Exception as exc:
            record['stopError'] = {'errorType': type(exc).__name__, 'message': str(exc)}
        container.remove(force=True, v=False)
        record['status'] = 'removed'
    except NotFound:
        record['status'] = 'already_removed'
    except Exception as exc:
        record.update(status='cleanup_failed', errorType=type(exc).__name__, message=str(exc))
        raise
    finally:
        (evidence / f'{name}-cleanup.json').write_bytes((json.dumps(record) + '\n').encode('utf-8'))


def archive_check_output(container, check_id):
    """Stream only the test output to disk, not a whole container filesystem."""
    target = runtime_root() / 'check-output' / f'{check_id}_{container.id[:12]}.tar'
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        chunks, _ = container.get_archive('/tmp/agent-check-output.txt')
        digest, size = hashlib.sha256(), 0
        with target.open('xb') as stream:
            for chunk in chunks:
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise ValueError('Full output exceeds 32 MiB archival cap; host excerpt retained')
                stream.write(chunk)
                digest.update(chunk)
        return {'path': str(target), 'bytes': size, 'sha256': digest.hexdigest(), 'status': 'archived'}
    except Exception as exc:
        return {'path': str(target), 'status': 'archive_failed', 'errorType': type(exc).__name__, 'message': str(exc)}


@contextmanager
def limited_official_containers(evidence, instance_id):
    """Apply limits before the official harness starts its selected container."""
    from docker.models.containers import ContainerCollection
    original = ContainerCollection.create
    created = []
    prefix = 'sweb.eval.' + instance_id.lower() + '.'

    def create(collection, *args, **kwargs):
        selected = str(kwargs.get('name', '')).startswith(prefix)
        if selected:
            if created:
                raise RuntimeError('Only one official evaluation container is allowed per invocation')
            kwargs.update(CONTAINER_LIMITS)
            kwargs['labels'] = {**(kwargs.get('labels') or {}), 'coding-agent.role': 'official-grading'}
        container = original(collection, *args, **kwargs)
        if selected:
            created.append(container)
        return container

    ContainerCollection.create = create
    try:
        yield
    finally:
        ContainerCollection.create = original
        errors = []
        for index, container in enumerate(created):
            try:
                remove_container(container, evidence, f'official-{index}')
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError('Official container cleanup failed; inspect cleanup records') from errors[0]
