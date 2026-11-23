"""Verified grading images with a reusable, content-keyed derived-image cache.

Imported by the run-local official launcher; importing starts no work. Docker
is imported only inside functions so offline regressions need no daemon.

The cache key is derived from the immutable official image ID, the target
base commit/tree and the restore recipe version. A verified derived image is
tagged deterministically; a later baseline / A/B / retry can reuse it with a
single probe instead of rebuilding the source tree. A cache hit still probes
and re-verifies the source tree, so reuse never skips verification. Any cache
lookup/tag failure degrades to the original full prepare path.
"""
import hashlib
import json
import re

if __package__:
    from .container_lifecycle import CONTAINER_LIMITS, MEMORY_BYTES, remove_container, save
else:
    from container_lifecycle import CONTAINER_LIMITS, MEMORY_BYTES, remove_container, save


# Bump when restore_dockerfile or the source checks below change; it is part of
# the cache key so an old cached image cannot be reused under new rules.
RESTORE_RECIPE_VERSION = 'v1'
CACHE_NAMESPACE = 'coding-agent-verified'


def cache_tag(image_id, commit, tree):
    """Deterministic local image tag for one verified (image, commit, tree, recipe)."""
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
        raise ValueError('Expected an immutable local image ID')
    material = json.dumps({'image': image_id, 'baseCommit': commit, 'tree': tree,
                           'recipe': RESTORE_RECIPE_VERSION},
                          sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(material.encode('utf-8')).hexdigest()[:64]
    return f'{CACHE_NAMESPACE}:{digest}'


def probe_image(client, image, evidence, name):
    """Read a disposable container, save evidence, then remove its instance."""
    container = client.containers.create(
        image.id, command=['sleep', 'infinity'], entrypoint=[], user='root',
        working_dir='/testbed', network_disabled=True,
        **CONTAINER_LIMITS,
        labels={'coding-agent.role': 'grading-preflight'})
    record = {'containerId': container.id, 'imageId': image.id}
    try:
        save(evidence / f'{name}-container.json', record)
        container.start()
        for key, args in (
                ('head', ['rev-parse', 'HEAD']),
                ('tree', ['rev-parse', 'HEAD^{tree}']),
                ('status', ['status', '--porcelain', '--untracked-files=all'])):
            result = container.exec_run(
                ['git', '-c', 'safe.directory=/testbed', *args],
                workdir='/testbed', user='root')
            if result.exit_code:
                raise ValueError(f'Image source probe failed: {key}, exit {result.exit_code}')
            record[key] = result.output.decode('utf-8').strip()
        result = container.exec_run(
            ['/opt/miniconda3/envs/testbed/bin/python', '-c',
             'import json,sys; print(json.dumps({"executable":sys.executable,"version":sys.version}))'])
        record['targetPython'] = {'exitCode': result.exit_code,
                                 'output': result.output.decode('utf-8', errors='replace')}
        save(evidence / f'{name}-source.json', record)
        return record
    finally:
        remove_container(container, evidence, name)


def restore_dockerfile(image_id, commit):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
        raise ValueError('Expected an immutable local image ID')
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Expected a full base commit')
    return (f'FROM {image_id}\nUSER root\nWORKDIR /testbed\n'
            f'RUN git -c safe.directory=/testbed reset --hard {commit}'
            ' && git -c safe.directory=/testbed clean -fd\n')


def _source_matches(record, commit, tree):
    return (record['head'] == commit and record['tree'] == tree and not record['status'])


def _tag_verified_image(client, image, tag, evidence):
    """Best-effort tagging; a cache write never blocks scoring."""
    try:
        image.tag(tag)
        save(evidence / 'image-cache.json', {'tag': tag, 'status': 'tagged', 'imageId': image.id})
    except Exception as exc:
        save(evidence / 'image-cache.json', {
            'tag': tag, 'status': 'tag_failed', 'imageId': image.id,
            'errorType': type(exc).__name__, 'message': str(exc)})


def _build_source_image(client, original, commit, evidence):
    context = evidence / 'image-override'
    context.mkdir()
    (context / 'Dockerfile').write_bytes(restore_dockerfile(original.id, commit).encode('utf-8'))
    image = original
    with (evidence / 'image-build.jsonl').open('xb') as stream:
        for event in client.api.build(path=str(context), decode=True, pull=False,
                                      network_mode='none', rm=True, forcerm=True,
                                      container_limits={'memory': MEMORY_BYTES, 'memswap': MEMORY_BYTES}):
            stream.write((json.dumps(event) + '\n').encode('utf-8'))
            stream.flush()
            if 'error' in event:
                raise ValueError('Source-restored image build failed; see image-build.jsonl')
            if 'aux' in event and 'ID' in event['aux']:
                image = client.images.get(event['aux']['ID'])
            match = re.search(r'Successfully built ([a-f0-9]+)', event.get('stream', ''))
            if match:
                image = client.images.get(match.group(1))
    if image.id == original.id:
        raise ValueError('Build did not return a source-restored image identity')
    return image


def resolve_verified_image(client, row, commit, tree, evidence):
    """Return a frozen-typed row bound to a verified, immutable grading image.

    A verified derived image is reused when its tag resolves and a fresh probe
    still matches the fixed source tree; otherwise the original build path runs
    and the result is cached for later arms, retries or baselines.
    """
    from docker.errors import ImageNotFound

    image_name = row.get('image')
    if not isinstance(image_name, str) or not image_name or image_name.startswith('-'):
        raise ValueError('Official record must provide its grading image')
    try:
        original = client.images.get(image_name)
    except ImageNotFound:
        original = client.images.pull(image_name)
    save(evidence / 'original-image.json', {
        'requested': image_name, 'id': original.id,
        'repoDigests': original.attrs.get('RepoDigests', []),
        'os': original.attrs.get('Os'), 'architecture': original.attrs.get('Architecture')})

    tag = cache_tag(original.id, commit, tree)
    cache = {'enabled': True, 'hit': False, 'reason': None, 'tag': tag}
    image = None
    try:
        cached = client.images.get(tag)
        # Use a distinct probe name so a failed cache probe never collides with
        # the full path's 'effective-*' evidence files (exclusive writes).
        candidate = probe_image(client, cached, evidence, 'cache')
        if _source_matches(candidate, commit, tree):
            image = cached
            cache.update(hit=True, reason='verified_reuse', imageId=cached.id)
            save(evidence / 'image-cache.json', {
                'tag': tag, 'status': 'reused', 'imageId': cached.id,
                'sourceVerifiedByProbe': True})
    except ImageNotFound:
        cache['reason'] = 'cold'
    except Exception as exc:
        # A stale, damaged or unreachable cache entry must degrade, never fail
        # a grading run. The full path below still verifies the source strictly.
        cache['reason'] = 'probe_failed'
        cache['errorType'] = type(exc).__name__
    if image is None:
        before = probe_image(client, original, evidence, 'original')
        image = original
        if not _source_matches(before, commit, tree):
            image = _build_source_image(client, original, commit, evidence)
        after = probe_image(client, image, evidence, 'effective')
        if not _source_matches(after, commit, tree):
            raise ValueError('Grading image does not match the fixed clean source tree')
        _tag_verified_image(client, image, tag, evidence)

    effective = {**row, 'image': image.id}
    save(evidence / 'source-verification.json', {
        'baseCommit': commit, 'expectedTree': tree, 'imageId': image.id,
        'sourceVerified': True, 'sourceRestored': image.id != original.id,
        'changedFields': ['image'] if effective != row else [],
        'probeContainers': 'removed_after_recording', 'cache': cache})
    return effective
