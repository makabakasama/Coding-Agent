"""Confirmed, retryable removal of a single trashed ordinary task round."""
from contextlib import ExitStack
from pathlib import Path
import shutil
import subprocess
import tempfile

from repo_agent.backend.schemas.task_contract import TASK_ID, TaskError
from repo_agent.infrastructure.file_io import exclusive, now, read_json, write_json


class TaskPurge:
    def __init__(self, store, execution, project_root):
        self.store, self.execution = store, execution
        self.project_root = Path(project_root).resolve()

    def get(self, task_id):
        path = self.store.purge_path(task_id)
        if path.exists():
            value = read_json(path)
            return {'taskId': task_id, 'purged': value['status'] == 'purged', 'status': value['status']}
        self.store.get(task_id)
        return {'taskId': task_id, 'purged': False, 'status': 'not_started'}

    def _safe_directory(self, path):
        path = Path(path)
        roots = [self.store.root, getattr(self.store, 'runs_root', self.store.root)]
        if path.resolve() != path or path in roots or not any(path.is_relative_to(root) for root in roots):
            raise TaskError(409, 'PURGE_PATH_UNSAFE', '任务材料目录无法安全确认，未执行清理')
        return path

    def _plan(self, task_id):
        task = self.store.get(task_id)
        directory = self._safe_directory(self.store.directory(task_id))
        if directory.exists() and read_json(directory/'task.json').get('taskId') != task_id:
            raise TaskError(409, 'PURGE_PATH_UNSAFE', '材料目录与本轮编号不一致')
        source = self.execution.projects.repository(task['contract']['projectId'], task['contract']['baseCommit'])
        if source.is_relative_to(directory):
            raise TaskError(409, 'PURGE_PATH_UNSAFE', '任务材料与原项目目录重叠，未执行清理')
        workspace = None
        identity = directory / 'preparation/identity.json'
        if task['contract'].get('workspaceMode', 'worktree') == 'worktree':
            record = directory / 'preparation/record.json'
            if record.exists():
                preparation = self.execution.preparation._read(record.parent, task_id)
                if preparation.get('preparationId'):
                    workspace = self.execution.preparation._workspace_path(task, preparation['preparationId'])
                    roots = [self.execution.preparation.workspace_root, getattr(self.store, 'project_workspaces',
                                                                               self.execution.preparation.workspace_root)]
                    if (workspace.resolve() != workspace or workspace in roots or source.is_relative_to(workspace)
                            or not any(workspace != root and workspace.is_relative_to(root) for root in roots)):
                        raise TaskError(409, 'PURGE_PATH_UNSAFE', '隔离副本目录无法安全确认')
                    if workspace.exists():
                        actual = self.execution.preparation._identity(source, workspace)
                        if identity.exists() and actual != read_json(identity):
                            raise TaskError(409, 'WORKSPACE_IDENTITY_MISMATCH', '隔离副本身份已变化')
        requirement = directory.parent / 'task.json' if directory.name.startswith('run_') else None
        external_logs = directory / 'execution/test-log-location.json'
        execution_id = None
        log_directory = None
        if external_logs.exists():
            state = self.execution._read_state(task_id)
            execution_id = state.get('executionId') if state else None
            if not isinstance(execution_id, str) or not TASK_ID.fullmatch(execution_id):
                raise TaskError(409, 'PURGE_PATH_UNSAFE', '外部测试日志缺少有效执行编号')
            log_directory = read_json(external_logs)['directory']
            self._external_logs(log_directory, execution_id, source)
        return {'taskId': task_id, 'status': 'purging', 'record': task, 'directory': str(directory),
                'source': str(source), 'workspace': str(workspace) if workspace else None,
                'externalLogs': log_directory, 'executionId': execution_id,
                'requirement': str(requirement) if requirement else None, 'startedAt': now()}

    def _external_logs(self, value, execution_id, source):
        if not isinstance(execution_id, str) or not TASK_ID.fullmatch(execution_id):
            raise TaskError(409, 'PURGE_PATH_UNSAFE', '外部日志执行编号无法确认')
        path = Path(value)
        expected = Path(tempfile.gettempdir()).resolve()/'coding-agent-test-logs'/execution_id
        if path != expected or path.resolve() != path or source.is_relative_to(path):
            raise TaskError(409, 'PURGE_PATH_UNSAFE', '外部测试日志目录无法安全确认')
        return path

    def _cleanup(self, plan, last):
        directory = self._safe_directory(plan['directory'])
        source = Path(plan['source'])
        if source.resolve() != source or source.is_relative_to(directory):
            raise TaskError(409, 'PURGE_PATH_UNSAFE', '原项目位于材料目录内，清理已停止')
        if plan['workspace']:
            workspace = Path(plan['workspace'])
            roots = [self.execution.preparation.workspace_root,
                     getattr(self.store, 'project_workspaces', self.execution.preparation.workspace_root)]
            if (workspace.resolve() != workspace or workspace in roots or source.is_relative_to(workspace)
                    or not any(workspace != root and workspace.is_relative_to(root) for root in roots)):
                raise TaskError(409, 'PURGE_PATH_UNSAFE', '隔离副本位置已变化，清理已停止')
            if workspace.exists():
                self.execution.preparation._identity(source, workspace)
                # Both absolute targets were verified against the allocated workspace roots.
                subprocess.run(['git', '-c', 'core.hooksPath=', '-C', str(source), 'worktree',
                                'remove', '--force', str(workspace)], check=True, capture_output=True, timeout=120)
        if plan.get('externalLogs'):
            logs = self._external_logs(plan['externalLogs'], plan['executionId'], source)
            if logs.exists():
                shutil.rmtree(logs)
        if directory.exists():
            shutil.rmtree(self._safe_directory(directory))
        if last and plan['requirement']:
            path = Path(plan['requirement'])
            self._safe_directory(path.parent)
            # Other rounds may have been created while this request was waiting.
            if path.exists() and not any(path.parent.glob('run_*/task.json')):
                if read_json(path).get('sourceFormat') != 'ordinary_v2':
                    raise TaskError(409, 'PURGE_PATH_UNSAFE', '需求材料格式无法确认')
                path.unlink()

    def apply(self, task_id, value):
        if type(value) is not dict or value != {'confirmTaskId': task_id}:
            raise TaskError(400, 'VALIDATION_ERROR', '彻底删除须确认完整任务编号')
        receipt = self.store.purge_path(task_id)
        self.store.root.mkdir(parents=True, exist_ok=True)
        with exclusive(self.store.root / ('lifecycle-' + task_id + '.lock')) as acquired:
            if not acquired:
                raise TaskError(409, 'TASK_BUSY', '本轮删除或恢复操作正在进行')
            try:
                plan = read_json(receipt) if receipt.exists() else self._plan(task_id)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise TaskError(409, 'PURGE_IDENTITY_UNCONFIRMED', '材料身份尚无法确认，请核对准备状态和目录后重试') from exc
            if plan['status'] == 'purged':
                return self.get(task_id)
            if not receipt.exists() and not self.store.is_deleted(task_id):
                raise TaskError(409, 'TRASH_REQUIRED', '请先将本轮移入回收站')
            directory = self._safe_directory(plan['directory'])
            with exclusive(self.store.root / ('material-' + task_id + '.lock')) as material_lock:
                if not material_lock:
                    raise TaskError(409, 'TASK_BUSY', '本轮材料正在恢复，请稍后重试')
                def cleanup(last):
                    if not receipt.exists():
                        with ExitStack() as locks:
                            for path in (directory/'preparation.lock', directory/'undo.lock'):
                                if path.parent.exists() and not locks.enter_context(exclusive(path)):
                                    raise TaskError(409, 'TASK_BUSY', '本轮正在准备或撤回')
                            if not hasattr(self.execution, 'queue'):
                                if not locks.enter_context(exclusive(self.store.root/'execution-global.lock')):
                                    raise TaskError(409, 'TASK_BUSY', '执行锁仍被占用')
                            state = self.execution._read_state(task_id)
                            if state and state['status'] != 'terminated':
                                raise TaskError(409, 'TASK_BUSY', '执行状态待核实，不能彻底删除')
                            receipt.parent.mkdir(parents=True, exist_ok=True)
                            write_json(receipt, plan)
                    self._cleanup(plan, last)
                relative = directory.relative_to(self.project_root).as_posix() if directory.is_relative_to(self.project_root) else None
                requirement = Path(plan['requirement']) if plan['requirement'] else None
                relative_requirement = (requirement.relative_to(self.project_root).as_posix()
                                        if requirement and requirement.is_relative_to(self.project_root) else None)
                try:
                    self.store.purge_run(task_id, cleanup, relative_directory=relative,
                                         relative_requirement=relative_requirement)
                    write_json(receipt, {'taskId': task_id, 'status': 'purged', 'finishedAt': now()})
                except TaskError as exc:
                    if receipt.exists():
                        raise TaskError(503, 'PURGE_INCOMPLETE', '清理未完成，可核对原因后重试。' + exc.message) from exc
                    raise
                except (OSError, subprocess.SubprocessError) as exc:
                    raise TaskError(503, 'PURGE_INCOMPLETE', '清理未完成，保留删除凭据，可重试彻底删除') from exc
                finally:
                    if hasattr(self.store, 'invalidate_lists'):
                        self.store.invalidate_lists()
            return self.get(task_id)
