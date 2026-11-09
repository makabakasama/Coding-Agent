"""One independent process with a bounded pool of synchronous task executions."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import uuid

from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.infrastructure.file_io import exclusive

logger = logging.getLogger('repo_agent.worker')


class WorkerPool:
    def __init__(self, queue, execution, *, workers=3, poll_seconds=1):
        if workers < 1 or poll_seconds <= 0:
            raise ValueError('Worker count and polling interval must be positive')
        self.queue, self.execution = queue, execution
        self.workers, self.poll_seconds = workers, poll_seconds
        self.worker_id = uuid.uuid4().hex

    def _execute(self, task_id, execution_id):
        logger.info('task=%s execution=%s started', task_id, execution_id)
        try:
            state = self.execution.run_claimed(task_id, execution_id)
            terminated = state is not None and state['status'] == 'terminated'
            reason = state.get('stopReason') if terminated else 'EXECUTION_UNCONFIRMED'
        except TaskError as exc:
            terminated, reason = False, exc.code
            logger.warning('task=%s execution=%s blocked reason=%s', task_id, execution_id, reason)
        except Exception as exc:
            terminated, reason = False, 'WORKER_EXECUTION_ERROR'
            logger.error('task=%s execution=%s error type=%s', task_id, execution_id, type(exc).__name__)
        try:
            self.queue.finish(task_id, self.worker_id, terminated=terminated, reason=reason)
        except Exception as exc:
            logger.error('task=%s execution=%s completion_not_saved type=%s', task_id, execution_id, type(exc).__name__)
            raise
        logger.info('task=%s execution=%s finished status=%s reason=%s',
                    task_id, execution_id, 'terminated' if terminated else 'unknown', reason)

    def run(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        self.queue.store.root.mkdir(parents=True, exist_ok=True)
        with self.queue.pool_session() as session:
            with exclusive(self.queue.store.root / 'execution-global.lock') as acquired:
                if not acquired or self.execution._owner():
                    raise TaskError(409, 'EXECUTION_UNRESOLVED', '现有执行仍在运行或待核实，Worker 池未启动')
                self.queue.abandon_claims()
                logger.info('worker=%s ready slots=%s', self.worker_id, self.workers)
                with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='agent-worker') as pool:
                    pending = set()
                    while not stop_event.is_set() or pending:
                        try:
                            self.queue.heartbeat(session, self.worker_id)
                            for future in list(pending):
                                if future.done():
                                    future.result()  # A DB completion failure must stop further claims.
                                    pending.remove(future)
                            while not stop_event.is_set() and len(pending) < self.workers:
                                claim = self.queue.claim(self.worker_id)
                                if claim is None:
                                    break
                                logger.info('task=%s execution=%s claimed worker=%s', *claim, self.worker_id)
                                pending.add(pool.submit(self._execute, *claim))
                            if pending or not stop_event.is_set():
                                # During graceful shutdown keep heartbeating active tasks.
                                threading.Event().wait(self.poll_seconds)
                        except KeyboardInterrupt:
                            logger.info('worker=%s draining active=%s', self.worker_id, len(pending))
                            stop_event.set()
                logger.info('worker=%s stopped', self.worker_id)
