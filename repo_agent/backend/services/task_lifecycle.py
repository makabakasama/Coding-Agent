"""Recoverable removal of ordinary task rounds from active history."""
from contextlib import ExitStack
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.infrastructure.file_io import exclusive


class TaskLifecycle:
    def __init__(self, store, execution):
        self.store, self.execution = store, execution

    def get(self, task_id):
        self.store.get(task_id)
        return {'taskId': task_id, 'deleted': self.store.is_deleted(task_id)}

    def change(self, task_id, value, *, actor=None):
        if type(value) is not dict or set(value) != {'deleted'} or type(value['deleted']) is not bool:
            raise TaskError(400, 'VALIDATION_ERROR', '请求须指定布尔值 deleted')
        self.store.purge_path(task_id)
        self.store.root.mkdir(parents=True, exist_ok=True)
        with exclusive(self.store.root / ('lifecycle-' + task_id + '.lock')) as acquired:
            if not acquired:
                raise TaskError(409, 'TASK_BUSY', '本轮删除或恢复操作正在进行')
            return self._change(task_id, value, actor=actor)

    def _change(self, task_id, value, *, actor=None):
        self.store.get(task_id)
        lock = self.execution._path(self.store.directory(task_id) / 'preparation.lock')
        with ExitStack() as locks:
            acquired = locks.enter_context(exclusive(lock))
            if not acquired:
                raise TaskError(409, 'TASK_BUSY', '任务正在准备，暂不能删除或恢复')
            if not hasattr(self.execution, 'queue'):
                acquired = locks.enter_context(exclusive(self.store.root / 'execution-global.lock'))
                if not acquired:
                    raise TaskError(409, 'TASK_BUSY', '执行锁仍被占用，暂不能删除或恢复')
            with self.execution._io():
                status = self.execution.get(task_id, check_readiness=False)['status']
                if value['deleted'] and status in ('running', 'finalizing', 'unknown'):
                    raise TaskError(409, 'TASK_BUSY', '任务仍在执行或待核实，暂不能删除')
                if actor is not None:
                    self.store.set_deleted(task_id, value['deleted'], audit=self.execution.queue.audit,
                                           actor=actor.account_id)
                else:
                    self.store.set_deleted(task_id, value['deleted'])
        return {'taskId': task_id, 'deleted': value['deleted']}
