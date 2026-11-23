"""Shared create/query use cases. Creating a task never starts an Agent."""
from __future__ import annotations

from datetime import datetime, timezone

from repo_agent.backend.schemas.task_contract import validate_contract
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.repositories.task_store import TaskStore
from repo_agent.backend.services.task_preparation import TaskPreparation
from repo_agent.backend.schemas.task_contract import TaskError
from pathlib import Path
from repo_agent.backend.services.task_execution import TaskExecution
from repo_agent.backend.repositories.task_history import TaskHistory
from repo_agent.backend.services.experiment_runs import ExperimentRuns


class TaskService:
    def close(self):
        self.store.close()

    def __init__(self, store: TaskStore, projects: TaskProjects, *, workspace_root=None,
                 model_factory=None, project_root=None, experiment_runner_factory=None,
                 chat_model_factory=None, chat_token_counter_factory=None):
        self.store, self.projects = store, projects
        self.preparation = TaskPreparation(store, projects, workspace_root or Path(__file__).resolve().parents[3] / "experiments/tasks")
        self.execution = TaskExecution(store, projects, self.preparation,
                                       model_factory=model_factory,
                                       token_counter_factory=chat_token_counter_factory)
        root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
        self.history = TaskHistory(root)
        self.chat = None
        self.chat_execution = None
        from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
        if isinstance(store, DatabaseTaskStore):
            from repo_agent.backend.repositories.database_task_history import DatabaseTaskHistory
            from repo_agent.backend.repositories.task_queue import TaskQueue
            from repo_agent.backend.services.queued_execution import QueuedExecution
            self.history = DatabaseTaskHistory(root, store)
            self.execution = QueuedExecution(self.execution, TaskQueue(store))
            from repo_agent.backend.repositories.chat_store import ChatStore
            from repo_agent.backend.services.chat_service import ChatService
            from repo_agent.backend.services.chat_execution import ChatExecution
            self.chat = ChatService(ChatStore(store), projects)
            self.chat.execution = self.execution
            self.chat_execution = ChatExecution(self.chat.store, projects=projects, task_service=self,
                                                model_factory=chat_model_factory,
                                                token_counter_factory=chat_token_counter_factory)
        self.experiments = ExperimentRuns(root, store, self.history, runner_factory=experiment_runner_factory)
        from repo_agent.backend.services.task_undo import TaskUndo
        self.undo = TaskUndo(self.execution)
        from repo_agent.backend.services.task_lifecycle import TaskLifecycle
        self.lifecycle = TaskLifecycle(store, self.execution)
        from repo_agent.backend.services.task_purge import TaskPurge
        self.purge = TaskPurge(store, self.execution, root)

    def prepare(self, task_id, value):
        if type(value) is not dict or value:
            raise TaskError(400, "VALIDATION_ERROR", "准备请求须为空 JSON 对象")
        return self.preparation.query(task_id, prepare=True)

    def create(self, value, *, requirement=None, task_id=None) -> dict:
        if isinstance(value,dict) and 'instruction' in value:
            from repo_agent.backend.services.simple_submission import SimpleSubmission
            value = SimpleSubmission(self.projects).normalize(value)
        contract = validate_contract(value)
        if task_id is not None:
            from repo_agent.backend.schemas.task_contract import TASK_ID
            if not TASK_ID.fullmatch(task_id):
                raise TaskError(400, 'VALIDATION_ERROR', '预分配任务编号无效')
            try:
                previous = self.store.get(task_id)
            except TaskError as exc:
                if exc.code != 'TASK_NOT_FOUND':
                    raise
            else:
                frozen = dict(previous['contract'])
                frozen['revision'] = frozen.pop('requestedRevision')
                frozen.pop('baseCommit')
                if frozen != contract:
                    raise TaskError(409, 'TASK_ID_REUSED', '预分配任务编号已有不同执行契约')
                return previous
        configured = self.projects._projects().get(contract['projectId'])
        if configured and contract.get('sourceKind','git') != configured.get('sourceKind','git'):
            raise TaskError(400,'INVALID_TARGET_TYPE','目标类型与本机登记不一致')
        commit = self.projects.resolve(contract["projectId"], contract["revision"])
        contract["requestedRevision"] = contract.pop("revision")
        contract["baseCommit"] = commit

        def build(task_id):
            return {"schemaVersion": 1, "taskId": task_id, "recordType": "actual",
                    "status": "created", "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "contract": contract}

        if task_id is not None:
            from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
            if not isinstance(self.store, DatabaseTaskStore):
                raise TaskError(409, 'DATABASE_REQUIRED', '会话执行需使用持久任务仓储')
            return self.store.create(build, requirement=requirement, task_id=task_id)
        return self.store.create(build, requirement=requirement) if requirement is not None else self.store.create(build)

    def ordinary_tasks(self, project):
        if project not in {p["projectId"] for p in self.projects.list()}:
            raise TaskError(404, "TASK_NOT_FOUND", "项目不存在")
        values = self.store.requirements(project) if hasattr(self.store, "requirements") else []
        values = sorted(values, key=lambda value: (
            datetime.fromisoformat(value['createdAt'].replace('Z', '+00:00')), value['taskKey']), reverse=True)
        counts = self.store.round_counts(project) if hasattr(self.store, 'round_counts') else {}
        tasks = [{**v, 'roundCount': counts[v['taskKey']] if v['taskKey'] in counts
                  else len(self.store.rounds(project, v['taskKey']))} for v in values]
        hidden = self.store.hidden_requirements(project) if hasattr(self.store, 'hidden_requirements') else set()
        return {'tasks': [task for task in tasks if task['taskKey'] not in hidden]}

    def ordinary_task(self, project, key):
        if not hasattr(self.store, "requirement"):
            raise TaskError(404, "TASK_NOT_FOUND", "需求不存在")
        _, value = self.store.requirement(project, key)
        return value

    def new_round(self, project, key, value):
        if type(value) is not dict or set(value) - {'maxSteps', 'workspaceMode'}:
            raise TaskError(400, "VALIDATION_ERROR", "新轮次请求只允许指定 maxSteps 和 workspaceMode")
        contract = dict(self.ordinary_task(project, key)["contract"])
        if 'maxSteps' in value:
            contract['maxSteps'] = value['maxSteps']
        if 'workspaceMode' in value:
            contract['workspaceMode'] = value['workspaceMode']
        contract["revision"] = contract.pop("requestedRevision")
        contract.pop("baseCommit")
        return self.create(contract, requirement=(project, key))

    def organization(self, task_id):
        self.get(task_id)
        return self.store.round_info(task_id) if hasattr(self.store, "round_info") else None

    def get(self, task_id: str) -> dict:
        return self.store.get(task_id)

    def list(self, *, deleted=False) -> list[dict]:
        cache = getattr(self.store, 'cache', None)
        if cache:
            return cache.versioned_json('task-list:' + str(deleted), lambda: self._list(deleted=deleted),
                                        generation='task-lists', ttl=lambda tasks: 1 if any(
                                            task['executionStatus'] in ('queued', 'running', 'finalizing')
                                            for task in tasks) else 30)
        return self._list(deleted=deleted)

    def _list(self, *, deleted=False):
        tasks = []
        records = self.store.latest(deleted=deleted)
        queue = getattr(self.execution, 'queue', None)
        statuses = queue.get_many([r['taskId'] for r in records]) if queue else None
        for record in records:
            task_id = record["taskId"]
            try:
                options = {'queue_status': statuses.get(task_id)} if statuses is not None else {}
                status = self.execution.get(task_id, check_readiness=False, **options)["status"]
            except TaskError:
                status = "unknown"
            try:
                result_available = any(self.execution._path(self.execution._directory(task_id) / name).is_file()
                                       for name in ('result.json', 'termination.json'))
            except (TaskError, OSError):
                result_available = False
            tasks.append({"taskId": task_id, "createdAt": record["createdAt"],
                          "projectId": record["contract"]["projectId"], "current": record["contract"]["current"],
                          "executionStatus": status, "resultAvailable": result_available})
        if deleted:
            for pending in self.store.pending_purges():
                record = pending['record']
                tasks.append({'taskId': record['taskId'], 'createdAt': record['createdAt'],
                              'projectId': record['contract']['projectId'], 'current': record['contract']['current'],
                              'executionStatus': 'unknown', 'resultAvailable': False, 'purgePending': True})
        return sorted(tasks, key=lambda task: (task['createdAt'], task['taskId']), reverse=True)[:50]
