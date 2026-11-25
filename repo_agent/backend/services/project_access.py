"""Project authorization and the transactional execution approval boundary."""
from pathlib import Path
import uuid

from repo_agent.backend.repositories.execution_audit import approval_proof, approved_chat_scope, fingerprint
from repo_agent.backend.repositories.project_access_store import ROLES, missing_project
from repo_agent.backend.schemas.chat_contract import validate_chat_id
from repo_agent.backend.schemas.task_contract import PROJECT_ID, TaskError
from repo_agent.backend.services.auth_service import normalize_login
from repo_agent.backend.services.review_delivery import review_base


def admin_required(identity):
    if not identity.admin:
        raise TaskError(403, 'ADMIN_REQUIRED', '此操作需要管理员账号')


class ProjectAccess:
    def __init__(self, service, repository):
        self.service = service
        self.repository = repository
        self.store = repository.store
        self.audit = service.audit
        repository.audit = self.audit

    def roles(self, identity):
        return None if identity.admin else self.repository.memberships(identity.account_id)

    def require(self, identity, project, role='observer'):
        if identity.admin:
            return
        with self.store._connection() as conn:
            self._require_member(conn, project, identity.account_id, role)

    def role_for(self, identity, project):
        if identity.admin:
            return 'admin'
        with self.store._connection() as conn:
            return self.repository.role(conn, project, identity.account_id)

    def _require_member(self, conn, project, account_id, role):
        actual = self.repository.role(conn, project, account_id)
        if actual is None:
            raise missing_project()
        if ROLES[actual] < ROLES[role]:
            raise TaskError(403, 'ROLE_REQUIRED', '此操作需要更高的项目权限')

    def require_task(self, identity, task_id, role='observer'):
        if not identity.admin:
            self.require(identity, self.store.project_for_task(task_id), role)

    def require_conversation(self, identity, chat, conversation_id, role='observer'):
        conversation = chat.get(conversation_id)
        project = conversation['workspaceRef']
        if project is not None:
            self.require(identity, project, role)
        else:
            owner = conversation.get('ownerAccountId')
            if owner != identity.account_id and (owner is not None or not identity.admin):
                raise TaskError(404, 'CONVERSATION_NOT_FOUND', '会话不存在')
        return conversation

    def _source(self, project):
        if not isinstance(project, str) or not PROJECT_ID.fullmatch(project):
            raise missing_project()
        projects = self.service.projects
        entry = projects._projects().get(project)
        if entry is None:
            raise missing_project()
        try:
            source = Path(entry['repo'])
            return str((projects.config_path.parent / source).resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise TaskError(409, 'PROJECT_SOURCE_CHANGED', '已登记仓库不可用') from exc

    def _check_source(self, conn, project, *, lock=False):
        pinned = self.repository.project(conn, project, lock=lock)
        if self._source(project) != pinned:
            raise TaskError(409, 'PROJECT_SOURCE_CHANGED', '当前仓库与启用时登记的路径不同')

    def check_source(self, project):
        with self.store._connection() as conn:
            self._check_source(conn, project)

    def enable(self, identity, project, value):
        admin_required(identity)
        if type(value) is not dict or set(value) != {'maintainer'}:
            raise TaskError(400, 'VALIDATION_ERROR', '请指定首位维护者登录名')
        source = self._source(project)
        self.service.projects.resolve(project, 'HEAD')
        self.repository.enable(project, source, normalize_login(value['maintainer']))

    def edit_member(self, identity, project, value):
        self.require(identity, project, 'maintainer')
        if type(value) is not dict or set(value) != {'login', 'role'}:
            raise TaskError(400, 'VALIDATION_ERROR', '请提供登录名和项目角色')
        role = value['role']
        if role is not None and (not isinstance(role, str) or role not in ROLES):
            raise TaskError(400, 'VALIDATION_ERROR', '项目角色无效')
        self.repository.change_member(project, identity, normalize_login(value['login']), role)

    @staticmethod
    def _enabled_account(conn, account_id):
        row = conn.execute("""SELECT admin FROM workbench_accounts
            WHERE account_id=%s AND enabled FOR SHARE""", (account_id,)).fetchone()
        if row is None:
            raise TaskError(409, 'REQUESTER_DISABLED', '账号已不可用')
        return row[0]

    def _require_submission(self, conn, identity, project):
        self.audit.lock(conn)
        self._check_source(conn, project, lock=True)
        if not self._enabled_account(conn, identity.account_id):
            self._require_member(conn, project, identity.account_id, 'developer')

    def _task_snapshot(self, task):
        info = self.service.organization(task['taskId'])
        if info is None or info['projectId'] != task['contract']['projectId']:
            raise TaskError(409, 'TASK_PROJECT_UNCONFIRMED', '无法核实任务的项目和需求归属')
        return {'contract': task['contract'], 'taskKey': info['taskKey'], 'reviewBase': info.get('reviewBase')}

    def request_task(self, identity, task_id, value):
        if type(value) is not dict or value:
            raise TaskError(400, 'VALIDATION_ERROR', '执行申请须为空 JSON 对象')
        project = self.store.project_for_task(task_id)
        with self.store._connection() as conn:
            self._require_submission(conn, identity, project)
            self.store.assert_active(task_id)
            existing = self.repository.task_request(project, task_id)
            if existing is not None and existing['kind'] == 'chat':
                raise TaskError(409, 'CHAT_EXECUTION_REQUEST_EXISTS', '该轮已关联 Chat 执行申请')
            if existing is None and self.service.execution.get(task_id, check_readiness=False,
                                                               fresh=True)['status'] != 'not_started':
                raise TaskError(409, 'TASK_ALREADY_STARTED', '该任务已经执行或入队')
            task = self.service.get(task_id)
            if task['contract']['projectId'] != project:
                raise missing_project()
            request = self.repository.create_request(conn, project, identity.account_id, 'task', task_id,
                                                     'execution', self._task_snapshot(task))
            if existing is None:
                self._audit_request(conn, request)
            return request

    def task_state(self, identity, task_id):
        project = self.store.project_for_task(task_id)
        self.require(identity, project)
        state = self.service.execution.get(task_id)
        request = self.repository.task_request(project, task_id)
        role = self.role_for(identity, project)
        enabled = project in self.repository.enabled_projects()
        can_request = (enabled and role in ('admin', 'developer', 'maintainer')
                       and state['status'] == 'not_started' and request is None)
        return {**state, 'canRequest': can_request, 'executionRequest': request}

    def request_chat(self, identity, chat, conversation, request):
        project = conversation['workspaceRef']
        if project is None:
            raise TaskError(400, 'WORKSPACE_REQUIRED', '修改需要关联项目')
        conversation_id = conversation['conversationId']
        with self.store._connection() as conn:
            self._require_submission(conn, identity, project)
            previous = self.repository.previous_request(conn, project, 'chat', conversation_id,
                                                        request['clientRequestId'])
            if previous is not None:
                if (previous['requesterId'] != identity.account_id
                        or previous['snapshot']['_clientRequestHash'] != chat.request_hash(request)):
                    raise TaskError(409, 'REQUEST_ALREADY_EXISTS', '会话请求编号已用于其他申请')
                return previous
            options = request['execution']
            if 'parentTaskId' in options:
                parent_id = validate_chat_id(options['parentTaskId'])
                if self.store.project_for_task(parent_id) != project:
                    raise missing_project()
            frozen = chat.freeze_request(conversation_id, request)
            record = self.repository.create_request(conn, project, identity.account_id, 'chat',
                                                     conversation_id, request['clientRequestId'], frozen)
            self._audit_request(conn, record)
            return record

    def _audit_request(self, conn, request):
        request_id = request['requestId']
        self.audit.event(conn, f'request:{request_id}:submitted', 'request_submitted', request=request_id,
                         task=request['targetId'] if request['kind'] == 'task' else None,
                         project=request['projectId'], actor=request['requesterId'])

    def decide(self, identity, project, request_id, value):
        admin_required(identity)
        validate_chat_id(request_id)
        if type(value) is not dict or set(value) != {'decision'} or value['decision'] not in ('approve', 'reject'):
            raise TaskError(400, 'VALIDATION_ERROR', '请选择放行或拒绝')
        approve = value['decision'] == 'approve'
        outcome = 'approved' if approve else 'rejected'
        task_id = None
        with self.store._connection() as conn:
            self.audit.lock(conn)
            self.repository.project(conn, project, lock=True)
            if not self._enabled_account(conn, identity.account_id):
                raise TaskError(403, 'ADMIN_REQUIRED', '此操作需要管理员账号')
            request = self.repository.request(conn, project, request_id, lock=True)
            if request['status'] == outcome:
                return request
            if request['status'] != 'pending':
                raise TaskError(409, 'REQUEST_ALREADY_DECIDED', '申请已有审批决定')
            execution = None
            if approve:
                proof = {**approval_proof(request, identity.account_id), 'kind': request['kind']}
                requester = request['requesterId']
                self._check_source(conn, project)
                if not self._enabled_account(conn, requester):
                    try:
                        self._require_member(conn, project, requester, 'developer')
                    except TaskError as exc:
                        raise TaskError(409, 'REQUESTER_PERMISSION_CHANGED', '申请人已不具备执行申请权限') from exc
                if request['kind'] == 'task':
                    task_id = request['targetId']
                    execution = self._approve_task(conn, project, task_id, request['snapshot'], proof)
                else:
                    execution = self._approve_chat(conn, project, request['targetId'], request['snapshot'], proof)
            result = self.repository.decide(conn, project, request_id, identity.account_id, outcome, execution)
            binding = execution or {}
            self.audit.event(conn, f'request:{request_id}:decision', f'request_{outcome}', request=request_id,
                             task=binding.get('taskId') or (request['targetId'] if request['kind'] == 'task' else None),
                             execution=binding.get('executionId'), project=project, actor=identity.account_id,
                             reason=None if approve else 'ADMIN_REJECTED')
        if task_id is not None:
            self.service.execution.queue._invalidate(task_id)
        return result

    def _approve_task(self, conn, project, task_id, snapshot, proof):
        self.store.assert_active(task_id)
        task = self.service.get(task_id)
        if task['contract']['projectId'] != project or self._task_snapshot(task) != snapshot:
            raise TaskError(409, 'EXECUTION_SCOPE_CHANGED', '任务执行范围与申请时不同')
        if snapshot['reviewBase']:
            parent = snapshot['reviewBase']
            if review_base(self.service, project, parent['parentTaskId']) != parent:
                raise TaskError(409, 'EXECUTION_SCOPE_CHANGED', '返工来源已变化')
        executor = self.service.execution
        state = executor.get(task_id, fresh=True)
        if state['status'] != 'not_started':
            raise TaskError(409, 'TASK_ALREADY_STARTED', '该任务已经执行或入队')
        if (not state['canStart'] and state.get('startBlockReason') == 'PREPARATION_REQUIRED'
                and task['contract'].get('workspaceMode') != 'direct'):
            state = {**state, 'canStart': True}
        repository_key, mode = executor.admission(task, state)
        execution_id = executor.queue.enqueue(task_id, repository_key=repository_key,
                                              workspace_mode=mode, connection=conn,
                                              authorization={**proof, 'scopeSha256': fingerprint(snapshot)})
        return {'taskId': task_id, 'executionId': execution_id, 'statusUrl': f'/api/tasks/{task_id}/execution'}

    def _approve_chat(self, conn, project, conversation_id, snapshot, proof):
        chat = self.service.chat
        conversation = chat.get(conversation_id)
        if conversation['workspaceRef'] != project or snapshot['execution']['projectId'] != project:
            raise TaskError(409, 'EXECUTION_SCOPE_CHANGED', '会话执行项目已变化')
        contract = snapshot['execution']
        commit = snapshot.get('frozenBaseCommit')
        if self.service.projects.resolve(project, commit or contract['revision']) != commit:
            raise TaskError(409, 'EXECUTION_SCOPE_CHANGED', '执行基线已变化')
        if snapshot.get('requirementKey'):
            key = snapshot['requirementKey']
            self.service.ordinary_task(project, key)
            if key in self.store.hidden_requirements(project):
                raise TaskError(409, 'REQUIREMENT_UNAVAILABLE', '需求已移入回收站')
        if snapshot.get('reviewBase'):
            parent = snapshot['reviewBase']
            if review_base(self.service, project, parent['parentTaskId']) != parent:
                raise TaskError(409, 'EXECUTION_SCOPE_CHANGED', '返工来源已变化')
        execution_id = uuid.uuid4().hex
        submitted = chat.store.submit(conversation_id, snapshot, connection=conn,
                                      authorization={**proof, 'scopeSha256': fingerprint(approved_chat_scope(snapshot))},
                                      execution_id=execution_id)
        if not submitted[0]:
            raise TaskError(409, 'CHAT_REQUEST_ALREADY_DISPATCHED', '会话请求已关联其他执行')
        _, turn = chat.turn_response(conversation_id, *submitted)
        return {'conversationId': conversation_id, 'turnId': turn['turnId'],
                'taskId': turn['turnId'], 'executionId': execution_id, 'statusUrl': turn['statusUrl']}
