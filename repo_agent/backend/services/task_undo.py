"""Conflict-checked direct-mode undo with an append-only result receipt."""
from contextlib import nullcontext
import hashlib
from pathlib import Path
import re

from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.task_execution_tools import TaskRepoTools
from repo_agent.infrastructure.file_io import exclusive, read_json, write_json, write_bytes, now


def digest(path):
    if path.resolve() != path:
        raise ValueError('Undo path is redirected')
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError('Undo file is not a regular file')
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TaskUndo:
    def __init__(self, execution):
        self.execution = execution

    def _inspect(self, task_id):
        task = self.execution.store.get(task_id)
        result = {'taskId': task_id, 'canUndo': False, 'status': 'unavailable', 'reason': None, 'conflicts': [], 'files': []}
        if task['contract'].get('workspaceMode') != 'direct':
            return result | {'reason': 'DIRECT_MODE_REQUIRED'}, None
        directory = self.execution._directory(task_id)
        manifest_path = directory / 'undo-manifest.json'
        state = self.execution._read_state(task_id)
        if not state or state['status'] != 'terminated':
            return result | {'reason': 'EXECUTION_NOT_TERMINATED'}, None
        if not manifest_path.exists():
            return result | {'reason': 'UNDO_EVIDENCE_UNAVAILABLE'}, None
        manifest = read_json(self.execution._path(manifest_path))
        if manifest.get('taskId') != task_id or manifest.get('baseCommit') != task['contract']['baseCommit']:
            raise ValueError('Invalid undo manifest identity')
        if manifest.get('complete') is not True:
            return result | {'reason': 'SCOPE_REVIEW_INCOMPLETE'}, None
        source = self.execution.projects.repository(task['contract']['projectId'], task['contract']['baseCommit'])
        from repo_agent.backend.services.direct_execution import DirectPreparation
        preparation = DirectPreparation(self.execution.store, self.execution.projects, source)
        identity = preparation._identity(source, source)
        saved_identity = read_json(directory.parent / 'preparation/identity.json')
        if identity != saved_identity:
            return result | {'reason': 'REPOSITORY_IDENTITY_CHANGED'}, None
        receipt_path = directory / 'undo-state.json'
        receipt = read_json(receipt_path) if receipt_path.exists() else {}
        if receipt.get('status') == 'undone':
            return result | {'status': 'undone', 'reason': 'ALREADY_UNDONE', 'files': receipt['files']}, None
        tools = TaskRepoTools(source, contract=task['contract'],excluded_roots=self.execution._runtime_paths(source))
        conflicts, entries = [], manifest.get('files')
        if not isinstance(entries, list) or len({e['path'] for e in entries}) != len(entries):
            raise ValueError('Invalid undo paths')
        for entry in entries:
            relative = entry['path']
            if not tools.editable(relative):
                raise ValueError('Undo path is outside allowed scope')
            for key in ('before', 'after'):
                value = entry[key]
                if value is not None and (not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value)):
                    raise ValueError('Invalid undo digest')
            backup = directory / 'baseline/files' / relative
            if entry['before'] is not None:
                if backup.resolve() != backup or digest(backup) != entry['before']:
                    raise ValueError('Undo baseline missing or changed')
            current = digest(tools._path(relative))
            expected = {entry['after']}
            if receipt.get('status') == 'applying':
                expected.add(entry['before'])
            if current not in expected:
                conflicts.append(relative)
        result.update(status='conflict' if conflicts else
                      'resumable' if entries and receipt.get('status') == 'applying' else 'ready' if entries else 'no_changes',
                      canUndo=bool(entries) and not conflicts, conflicts=conflicts,
                      files=[e['path'] for e in entries], reason='FILES_CHANGED' if conflicts else None)
        return result, (directory, source, entries)

    def get(self, task_id):
        with self.execution._io():
            return self._inspect(task_id)[0]

    def apply(self, task_id, value):
        if type(value) is not dict or set(value) != {'executionId'}:
            raise TaskError(400, 'VALIDATION_ERROR', '撤回请求必须指定 executionId')
        self.execution.store.get(task_id)
        directory = self.execution._directory(task_id)
        with self.execution._io():
            with exclusive(directory.parent / 'undo.lock') as acquired:
                if not acquired:
                    raise TaskError(409, 'UNDO_BUSY', '本轮撤回正在处理')
                state = self.execution._read_state(task_id)
                if not state or value['executionId'] != state.get('executionId'):
                    raise TaskError(409, 'EXECUTION_CHANGED', '执行编号不匹配，请重新查询')
                task = self.execution.store.get(task_id)
                source = self.execution.projects.repository(task['contract']['projectId'], task['contract']['baseCommit'])
                key = hashlib.sha256(str(source).casefold().encode()).hexdigest()
                queue = getattr(self.execution, 'queue', None)
                with queue.repository_idle(key) if queue else nullcontext():
                    status, context = self._inspect(task_id)
                    if status['status'] == 'undone':
                        return status
                    if not status['canUndo']:
                        raise TaskError(409, status['reason'] or 'UNDO_UNAVAILABLE',
                                        '无法撤回，请核对执行状态、基线及冲突文件',
                                        [{'field': 'files', 'message': path} for path in status['conflicts']])
                    directory, source, entries = context
                    receipt = {'taskId': task_id, 'executionId': value['executionId'], 'status': 'applying',
                               'files': status['files'], 'updatedAt': now()}
                    write_json(directory / 'undo-state.json', receipt)
                    for entry in entries:
                        path = source / entry['path']
                        # Recheck immediately before each mutation. The repository
                        # scheduling lock excludes concurrent Agent executions.
                        current = digest(path)
                        if current == entry['before']:
                            continue
                        if current != entry['after']:
                            raise TaskError(409, 'FILES_CHANGED', '文件在撤回过程中发生变化，请重新核对')
                        if entry['before'] is None:
                            path.unlink()
                        else:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            write_bytes(path, (directory / 'baseline/files' / entry['path']).read_bytes())
                    write_json(directory / 'undo-state.json', receipt | {'status': 'undone', 'updatedAt': now()})
                    return self._inspect(task_id)[0]
