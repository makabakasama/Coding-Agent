"""Conversation use cases. Submitting a turn only persists queued work."""
import codecs
import hashlib
import json
from pathlib import PurePosixPath
import re

from repo_agent.backend.schemas.chat_contract import (
    validate_chat_id, validate_conversation, validate_page, validate_sequence, validate_turn,
)
from repo_agent.backend.schemas.task_contract import TaskError, validate_contract


class ChatService:
    def __init__(self, store, projects, task_service=None):
        self.task_service = task_service
        self.store, self.projects = store, projects
        self.execution = None

    def create(self, value, *, owner_id=None):
        contract = validate_conversation(value)
        workspace = contract['workspaceRef']
        if workspace is not None and workspace not in self.projects._projects():
            raise TaskError(400, 'UNKNOWN_PROJECT', '会话工作区须为已登记项目')
        return (self.store.create(contract, owner_id=owner_id) if owner_id is not None
                else self.store.create(contract))

    def requirement_discussion(self, project, key):
        item = self.task_service.ordinary_task(project, key)
        if item.get('mode') != 'ordinary' or key in self.task_service.store.hidden_requirements(project):
            raise TaskError(409, 'REQUIREMENT_UNAVAILABLE', '该需求当前不可讨论')
        contract = item['contract']
        return self.store.create({'workspaceRef': project, 'title': item['title'][:120],
                                  'requirement': {'taskKey': key, 'current': contract['current'],
                                                  'expected': contract['expected']}})

    def get(self, conversation_id):
        return self.store.get(conversation_id)

    def list(self, *, cursor=None, limit=50, project=None, project_ids=None, owner_id=None):
        if owner_id is not None:
            return self.store.list(cursor=cursor, limit=limit, project=project,
                                   project_ids=project_ids, owner_id=owner_id)
        if project is not None:
            if project not in self.projects._projects():
                raise TaskError(404, 'UNKNOWN_PROJECT', '项目不存在')
            return self.store.list(cursor=cursor, limit=limit, project=project)
        return self.store.list(cursor=cursor, limit=limit)

    def messages(self, conversation_id, *, after_seq=0, limit=50):
        return self.store.messages(conversation_id, after_seq=after_seq, limit=limit)

    def turn(self, conversation_id, turn_id):
        turn = self.store.turn(conversation_id, turn_id)
        if (self.execution is not None and turn['mode'] == 'execute'
                and turn['status'] == 'running' and turn.get('stage') == 'execution'
                and turn.get('taskId')):
            try:
                task = self.execution.get(turn['taskId'], check_readiness=False, fresh=True)
            except TaskError as exc:
                if exc.code in ('TASK_NOT_FOUND', 'TASK_PURGED'):
                    return {**turn, 'status': 'unknown', 'stopReason': 'TASK_UNAVAILABLE'}
                raise
            if task['status'] == 'unknown':
                return {**turn, 'status': 'unknown', 'stopReason': 'TASK_EXECUTION_UNKNOWN'}
        return turn

    def events(self, conversation_id, turn_id, *, after=0, limit=50):
        return self.store.events(conversation_id, turn_id, after=after, limit=limit)

    def artifact(self, conversation_id, turn_id, artifact_id, *, offset=0, limit=16000):
        validate_sequence(offset)
        validate_page(limit, maximum=16000)
        turn = self.store.turn(conversation_id, turn_id)
        if (artifact_id not in {'context', 'request', 'response', 'reply', 'trace', 'result'}
                and not re.fullmatch(r'(?:request|response)[2-8]', artifact_id)):
            raise TaskError(404, 'ARTIFACT_NOT_FOUND', '会话材料不存在')
        reference = turn.get('artifacts', {}).get(artifact_id)
        if (not isinstance(reference, dict) or not isinstance(reference.get('path'), str)
                or not isinstance(reference.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', reference['sha256'])):
            raise TaskError(404, 'ARTIFACT_NOT_FOUND', '本轮未登记该材料')
        relative = PurePosixPath(reference['path'])
        parts = relative.parts
        numbered = re.fullmatch(r'(request|response)([2-8])', artifact_id)
        filename = (f'{numbered[1]}-{numbered[2]}.json' if numbered
                    else artifact_id + '.json')
        if (len(parts) != 5 or parts[:3] != ('chat', conversation_id, turn_id)
                or parts[4] != filename):
            raise TaskError(409, 'ARTIFACT_INVALID', '会话材料路径与轮次不匹配')
        validate_chat_id(parts[3])
        path = self.store.store.runs_root.joinpath(*parts)
        if path.resolve() != path:
            raise TaskError(409, 'ARTIFACT_INVALID', '会话材料路径无法核实')
        digest, decoder = hashlib.sha256(), codecs.getincrementaldecoder('utf-8')()
        fragments, total = [], 0
        try:
            with path.open('rb') as stream:
                while True:
                    raw = stream.read(65536)
                    digest.update(raw)
                    text = decoder.decode(raw, final=not raw)
                    start = max(0, offset - total)
                    end = min(len(text), offset + limit - total)
                    if end > start:
                        fragments.append(text[start:end])
                    total += len(text)
                    if not raw:
                        break
        except (OSError, UnicodeError) as exc:
            raise TaskError(503, 'ARTIFACT_UNAVAILABLE', '会话材料缺失或无法读取') from exc
        if digest.hexdigest() != reference['sha256']:
            raise TaskError(409, 'ARTIFACT_CHANGED', '材料内容与登记哈希不一致')
        if offset > total:
            raise TaskError(400, 'INVALID_PAGINATION', '材料游标超出文本范围')
        content = ''.join(fragments)
        next_offset = offset + len(content)
        return {'conversationId': conversation_id, 'turnId': turn_id, 'artifactId': artifact_id,
                'content': content, 'offset': offset, 'nextOffset': next_offset,
                'totalChars': total, 'hasMore': next_offset < total,
                'sha256': reference['sha256'], 'integrityChecked': True,
                'redacted': reference.get('redacted'), 'credentialRedactionApplied': True}

    def submit(self, conversation_id, value, *, authorization=None):
        request = validate_turn(value)
        digest = self.request_hash(request)
        replay = self.store.replay(conversation_id, request['clientRequestId'], digest)
        if replay is not None:
            turn, conversation = replay
            return self.turn_response(conversation_id, False, turn, conversation)
        request = self.freeze_request(conversation_id, request)
        submitted = (self.store.submit(conversation_id, request, authorization=authorization)
                     if authorization is not None else self.store.submit(conversation_id, request))
        return self.turn_response(conversation_id, *submitted)

    @staticmethod
    def request_hash(request):
        semantic = {key: item for key, item in request.items() if key != 'expectedRevision'}
        return hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    def freeze_request(self, conversation_id, request):
        request = {**request, '_clientRequestHash': self.request_hash(request)}
        if request['mode'] == 'execute':
            conversation = self.store.get(conversation_id)
            workspace = conversation['workspaceRef']
            if workspace is None:
                raise TaskError(400, 'WORKSPACE_REQUIRED', '修改需要关联项目')
            project = self.projects._projects().get(workspace)
            if project is None:
                raise TaskError(400, 'UNKNOWN_PROJECT', '会话关联项目已不可用')
            options = dict(request['execution'])
            bound_key = (conversation.get('requirement') or {}).get('taskKey')
            if bound_key and 'parentTaskId' not in options:
                if options.get('requirementKey', bound_key) != bound_key:
                    raise TaskError(409, 'REQUIREMENT_MISMATCH', '请从目标需求打开讨论')
                options['requirementKey'] = bound_key
            if 'requirementKey' in options:
                key = options.pop('requirementKey')
                if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,160}', key) or 'parentTaskId' in options:
                    raise TaskError(400, 'VALIDATION_ERROR', '请选择一项已有需求或一版交付')
                requirement = self.task_service.ordinary_task(workspace, key)
                if requirement.get('projectId') != workspace or requirement.get('mode') != 'ordinary':
                    raise TaskError(409, 'REQUIREMENT_MISMATCH', '当前项目的普通需求不存在')
                if key in self.task_service.store.hidden_requirements(workspace):
                    raise TaskError(409, 'REQUIREMENT_UNAVAILABLE', '需求已移入回收站')
                request['requirementKey'] = key
            if 'parentTaskId' in options:
                from repo_agent.backend.services.review_delivery import review_base
                parent_id = validate_chat_id(options.pop('parentTaskId'))
                base = review_base(self.task_service, workspace, parent_id)
                if bound_key and base['taskKey'] != bound_key:
                    raise TaskError(409, 'REQUIREMENT_MISMATCH', '所选交付属于其他需求')
                if 'workspaceMode' in options and options['workspaceMode'] != base['workspaceMode']:
                    raise TaskError(409, 'REVIEW_MODE_MISMATCH', '返工的工作方式须与原轮次一致')
                request['reviewBase'] = base
                options.update(workspaceMode=base['workspaceMode'], revision=base['revision'] or 'CURRENT')
                if base['targetFile']:
                    options['targetFile'] = base['targetFile']
                elif 'targetFile' in options:
                    raise TaskError(400, 'REVIEW_SCOPE_MISMATCH', '返工的工作区基线须与原轮次一致')
            if set(options) - {'kind', 'expected', 'allowedPaths', 'allowTests', 'checkTargets',
                               'workspaceMode', 'revision', 'maxSteps', 'targetFile', 'allowTemporaryChecks'}:
                raise TaskError(400, 'VALIDATION_ERROR', '执行设置包含不支持的字段')
            if options.get('kind', 'fix_bug') not in ('fix_bug', 'add_feature'):
                raise TaskError(400, 'VALIDATION_ERROR', '修改类型须为修复缺陷或新增功能')
            request['execution'] = validate_contract({
                **options, 'projectId': workspace, 'sourceKind': project.get('sourceKind', 'git'),
                'current': request['content'], 'expected': options.get('expected', request['content']),
                'revision': options.get('revision', 'HEAD'),
                'workspaceMode': options.get('workspaceMode', 'direct'),
                'allowTests': options.get('allowTests', False),
                'checkTargets': options.get('checkTargets', []),
                'maxSteps': options.get('maxSteps', 30),
                'kind': options.get('kind', 'fix_bug'),
            })
            # Freeze a Git base before the turn waits behind earlier messages.
            frozen_commit = self.projects.resolve(workspace, request['execution']['revision'])
            if frozen_commit is not None:
                request['frozenBaseCommit'] = frozen_commit
        return request

    @staticmethod
    def turn_response(conversation_id, created, turn, conversation):
        base = f'/api/conversations/{conversation_id}/turns/{turn["turnId"]}'
        return (202 if created else 200), {
            **turn, 'conversationRevision': conversation['revision'],
            'statusUrl': base, 'eventsUrl': base + '/events',
        }
