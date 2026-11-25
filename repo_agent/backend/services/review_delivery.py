"""Freeze the reviewed delivery as the starting point of a follow-up run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.infrastructure.file_io import read_json


def snapshot_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def review_base(service, project, parent_id):
    service.store.assert_active(parent_id)
    parent = service.store.get(parent_id)['contract']
    info = service.organization(parent_id)
    if not info or info['projectId'] != project or parent['projectId'] != project:
        raise TaskError(409, 'REVIEW_PROJECT_MISMATCH', '所选交付不属于当前项目')
    state = service.execution.get(parent_id, check_readiness=False, fresh=True)
    if state['status'] != 'terminated':
        raise TaskError(409, 'REVIEW_NOT_READY', '请等待原轮次结束')
    result = service.execution.result(parent_id)
    export = result.get('patchExport') or {}
    review = result.get('review') or {}
    if (result.get('executionId') != state.get('executionId') or export.get('status') != 'saved'
            or not export.get('sha256') or review.get('status') != 'passed'):
        raise TaskError(409, 'REVIEW_BASE_UNAVAILABLE', '原轮次缺少完整的补丁或范围审查材料')
    directory = service.execution._directory(parent_id)
    mode = parent.get('workspaceMode', 'worktree')
    base = {'taskKey': info['taskKey'], 'parentTaskId': parent_id, 'patchSha256': export['sha256'],
            'workspaceMode': mode, 'revision': parent['baseCommit'], 'targetFile': parent.get('targetFile')}
    try:
        patch = service.execution._path(directory / 'candidate.patch').read_bytes()
        if hashlib.sha256(patch).hexdigest() != export['sha256']:
            raise ValueError('Patch changed')
        if mode == 'direct':
            files = read_json(service.execution._path(directory / 'baseline/manifest.json'))
            manifest = read_json(service.execution._path(directory / 'undo-manifest.json'))
            if (not manifest['complete'] or manifest['taskId'] != parent_id
                    or manifest['baseCommit'] != parent['baseCommit']
                    or {e['path'] for e in manifest['files']} != set(result['review']['changedPaths'])):
                raise ValueError('Incomplete reviewed baseline')
            for entry in manifest['files']:
                if files.get(entry['path']) != entry['before']:
                    raise ValueError('Inconsistent baseline')
                if entry['after'] is None:
                    files.pop(entry['path'], None)
                else:
                    files[entry['path']] = entry['after']
            base['snapshotSha256'] = snapshot_digest(files)
        else:
            repo = service.projects.repository(project, parent['baseCommit'])
            base['revision'] = reviewed_commit(repo, parent['baseCommit'], patch)
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise TaskError(409, 'REVIEW_BASE_UNAVAILABLE', '无法核实原轮次的交付版本') from exc
    return base


def reviewed_commit(repo, commit, patch):
    if not patch:
        return commit
    # Build a detached Git object from the saved patch without touching any checkout or branch.
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index'),
               'GIT_AUTHOR_NAME': 'Coding Agent', 'GIT_COMMITTER_NAME': 'Coding Agent',
               'GIT_AUTHOR_EMAIL': 'review@localhost', 'GIT_COMMITTER_EMAIL': 'review@localhost',
               'GIT_AUTHOR_DATE': '2000-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00Z'}
        def git(*args, data=None):
            return subprocess.run(['git', '-C', str(repo), *args], env=env, input=data,
                                  capture_output=True, check=True, timeout=30).stdout.strip()
        git('read-tree', commit)
        git('apply', '--cached', '--whitespace=nowarn', '-', data=patch)
        tree = git('write-tree').decode('ascii')
        return git('commit-tree', tree, '-p', commit, '-m', 'Reviewed delivery').decode('ascii')
