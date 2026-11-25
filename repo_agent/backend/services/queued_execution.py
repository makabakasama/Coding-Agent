"""Execution API that only enqueues work. Execution evidence stays in TaskExecution."""
import hashlib
import subprocess
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.task_execution import git

_UNSET = object()


class QueuedExecution:
    def __init__(self, execution, queue):
        self.execution, self.queue = execution, queue

    def __getattr__(self, name):
        return getattr(self.execution, name)

    def _readiness(self, task):
        try:
            workspace, _ = self.execution._workspace(task)
            if task['contract'].get('workspaceMode') != 'direct' and git(workspace, 'status', '--porcelain=v1', '-z', '--ignored=matching', '--untracked-files=all'):
                return 'WORKSPACE_DIRTY'
            if self.execution._owner():
                return 'EXECUTION_UNRESOLVED'
            return None
        except TaskError as exc:
            return exc.code
        except (OSError, ValueError, subprocess.SubprocessError):
            return 'WORKSPACE_UNAVAILABLE'

    def get(self, task_id, *, check_readiness=True, fresh=False, queue_status=_UNSET):
        task = self.execution.store.get(task_id)
        queued = self.queue.get(task_id, fresh=fresh) if queue_status is _UNSET else queue_status
        if queued is None:
            with self.execution._io():
                state = self.execution._read_state(task_id)
            if state is not None:
                return self.execution.get(task_id, check_readiness=check_readiness)
            reason = self._readiness(task) if check_readiness else None
            return {'schemaVersion': 1, 'taskId': task_id, 'status': 'not_started',
                    'canStart': reason is None, 'startBlockReason': reason,
                    'source': self.execution.source, 'isExecutionSandbox': False, 'resultAvailable': False}
        with self.execution._io():
            state = self.execution._read_state(task_id)
        status = queued['status']
        denied = status == 'terminated' and (queued.get('reason') or '').startswith('AUTH_')
        if status == 'running' and state and state.get('executionId') == queued['executionId']:
            status = state['status']
        if status == 'terminated' and not denied and (not state or state['status'] != 'terminated'):
            status = 'unknown'
        return {**(state or {}), 'schemaVersion': 1, 'taskId': task_id,
                'executionId': queued['executionId'], 'status': status, 'canStart': False,
                'startBlockReason': 'ALREADY_STARTED', 'source': self.execution.source,
                'isExecutionSandbox': False, 'queuedAt': queued['queuedAt'],
                'phase': (state or {}).get('phase', 'queue'),
                'activity': 'waiting_worker' if status == 'queued' else 'unconfirmed' if status == 'unknown' else (state or {}).get('activity', 'starting'),
                'startedAt': queued['startedAt'], 'finishedAt': queued['finishedAt'],
                'liveness': 'confirmed' if status in ('running', 'finalizing') else 'stopped' if status == 'terminated' else 'unconfirmed',
                'stopReason': queued['reason'] or (state or {}).get('stopReason'),
                'resultAvailable': bool((state or {}).get('resultAvailable')),
                **({'authorizationDenied': True} if denied else {})}

    def start(self, task_id, value, *, authorization=None, execution_id=None):
        if type(value) is not dict or value:
            raise TaskError(400, 'VALIDATION_ERROR', '启动请求须为空 JSON 对象')
        self.execution.store.assert_active(task_id)
        state = self.get(task_id, fresh=True)
        if state['status'] != 'not_started':
            if authorization is not None and state.get('executionId') != execution_id:
                raise TaskError(409, 'AUTH_SCOPE_MISMATCH', '编码任务已有其他执行编号')
            return 200, state
        task = self.execution.store.get(task_id)
        repository_key, mode = self.admission(task, state)
        if authorization is not None:
            authorization = self.queue.audit.bind_bridge_task(task, authorization, execution_id)
        self.queue.enqueue(task_id, repository_key=repository_key, workspace_mode=mode,
                           authorization=authorization, execution_id=execution_id)
        return 202, self.get(task_id)

    def admission(self, task, state):
        if not state['canStart']:
            raise TaskError(409, state['startBlockReason'], '任务当前不可入队')
        contract = task['contract']
        source = self.execution.projects.repository(contract['projectId'], contract['baseCommit'])
        repository_key = hashlib.sha256(str(source).casefold().encode()).hexdigest()
        return repository_key, contract.get('workspaceMode', 'worktree')

    def recovery(self, task_id, value=None):
        if self.queue.get(task_id) is None:
            return self.execution.recovery(task_id, value)
        if value is not None:
            raise TaskError(409, 'QUEUE_MANAGED', '该轮由队列管理，请核对记录后建立新轮次')
        return {'owner': None, 'canRelease': False, 'reason': 'QUEUE_MANAGED', 'released': None}
