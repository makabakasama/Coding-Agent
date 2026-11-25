"""Conversation routes on the existing local HTTP handler."""
import re
import json
import time
from urllib.parse import parse_qs, urlsplit

from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.routers.auth_http import authenticate_request, require_admin
from repo_agent.backend.schemas.chat_contract import validate_conversation, validate_turn


PROJECT_EVENT_FIELDS = {'seq', 'at', 'type', 'turnId', 'decisionIndex', 'attempt',
                        'outcome', 'action', 'stage', 'status', 'taskId', 'executionId'}


def _project_events(handler, events):
    if getattr(handler, 'access', None) is None or handler.identity.admin:
        return events
    result = []
    for event in events:
        public = {key: value for key, value in event.items() if key in PROJECT_EVENT_FIELDS}
        if event.get('type') == 'model_text':
            public['text'] = event['text']
        observation = event.get('result')
        if isinstance(observation, dict) and type(observation.get('ok')) is bool:
            public['result'] = {'ok': observation['ok']}
        result.append(public)
    return result


def _authorize_stream(handler, chat, conversation_id):
    access = getattr(handler, 'access', None)
    if access is None:
        require_admin(handler)
    else:
        handler.identity = authenticate_request(handler)
        access.require_conversation(handler.identity, chat, conversation_id)


def _stream_events(handler, chat, conversation_id, turn_id, after):
    """Replay durable events, then tail them over a bounded SSE connection."""
    # Validate ownership and cursor before sending streaming headers.
    _authorize_stream(handler, chat, conversation_id)
    page = chat.events(conversation_id, turn_id, after=after, limit=100)
    handler.send_response(200)
    for key, value in {'Content-Type': 'text/event-stream; charset=utf-8',
                       'Cache-Control': 'no-cache, no-transform', 'Connection': 'close',
                       'X-Accel-Buffering': 'no', 'X-Content-Type-Options': 'nosniff'}.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.close_connection = True
    deadline = time.monotonic() + 20
    next_auth_check = time.monotonic() + 1
    try:
        handler.wfile.write(b'retry: 500\n\n')
        while True:
            if page['events'] or time.monotonic() >= next_auth_check:
                _authorize_stream(handler, chat, conversation_id)
                next_auth_check = time.monotonic() + 1
            for event in _project_events(handler, page['events']):
                after = event['seq']
                data = json.dumps(event, ensure_ascii=False)
                handler.wfile.write(f'id: {after}\ndata: {data}\n\n'.encode('utf-8'))
            handler.wfile.flush()
            if not page['hasMore']:
                turn = chat.turn(conversation_id, turn_id)
                if turn['status'] in ('completed', 'failed', 'unknown'):
                    # Recheck after the terminal read so the final committed events are drained.
                    page = chat.events(conversation_id, turn_id, after=after, limit=100)
                    if page['events']:
                        continue
                    _authorize_stream(handler, chat, conversation_id)
                    handler.wfile.write(b'event: stream_done\ndata: {}\n\n')
                    handler.wfile.flush()
                    return
                if time.monotonic() >= deadline:
                    return
                handler.wfile.write(b': heartbeat\n\n')
                handler.wfile.flush()
                time.sleep(0.15)
            page = chat.events(conversation_id, turn_id, after=after, limit=100)
    except (OSError, TaskError):
        # A disconnected reader does not cancel the Worker or change turn status.
        return


def _query(handler, allowed):
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise TaskError(400, 'INVALID_PAGINATION', '分页参数无效或重复')
    result = {}
    for name, values in query.items():
        value = values[0]
        if name in ('cursor','project'):
            result[name] = value
        else:
            if not value or not value.isascii() or not value.isdigit() or len(value) > 19:
                raise TaskError(400, 'INVALID_PAGINATION', '分页参数须为非负整数')
            result[name] = int(value)
    return result


def dispatch_chat(handler, path):
    match = re.fullmatch(
        r'/api/conversations(?:/([^/]+)(?:/(messages|turns)(?:/([^/]+)(?:/(events)|/artifacts/([^/]+))?)?)?)?', path)
    if match is None:
        raise TaskError(404, 'ROUTE_NOT_FOUND', '会话接口不存在')
    conversation_id, part, turn_id, events, artifact_id = match.groups()
    if part == 'messages' and turn_id is not None:
        raise TaskError(404, 'ROUTE_NOT_FOUND', '会话接口不存在')
    allowed = ('GET',)
    if conversation_id is None:
        allowed = ('GET', 'POST')
    elif part == 'turns' and turn_id is None:
        allowed = ('POST',)
    if handler.command not in allowed:
        handler._json(405, TaskError(405, 'METHOD_NOT_ALLOWED', '该接口不支持此方法').response(),
                      {'Allow': ', '.join(allowed)})
        return
    chat = getattr(handler.service, 'chat', None)
    if chat is None:
        raise TaskError(503, 'CHAT_UNAVAILABLE', '会话服务需要 PostgreSQL 存储')
    access = handler.access
    if access is not None and conversation_id is not None:
        conversation = access.require_conversation(handler.identity, chat, conversation_id,
                                                   'developer' if handler.command == 'POST' else 'observer')
    if conversation_id is None:
        if handler.command == 'POST':
            _query(handler, set())
            value = handler._body()
            if access is not None:
                project = validate_conversation(value)['workspaceRef']
                if project is not None:
                    access.require(handler.identity, project, 'developer')
                    access.check_source(project)
                record = chat.create(value, owner_id=handler.identity.account_id)
            else:
                record = chat.create(value)
            handler._json(201, record, {'Location': '/api/conversations/' + record['conversationId']})
        else:
            query = _query(handler, {'cursor', 'limit', 'project'})
            if access is not None:
                if 'project' in query:
                    access.require(handler.identity, query['project'])
                query.update(project_ids=access.roles(handler.identity), owner_id=handler.identity.account_id)
            handler._json(200, chat.list(**query))
    elif part is None:
        _query(handler, set())
        if access is None:
            handler._json(200, chat.get(conversation_id))
        else:
            project = conversation['workspaceRef']
            role = access.role_for(handler.identity, project) if project is not None else None
            if project is not None and role is None:
                from repo_agent.backend.repositories.project_access_store import missing_project
                raise missing_project()
            handler._json(200, {**conversation, 'canPost': project is None
                                or role in ('admin', 'developer', 'maintainer')})
    elif part == 'messages':
        query = _query(handler, {'afterSeq', 'limit'})
        handler._json(200, chat.messages(conversation_id, after_seq=query.get('afterSeq', 0),
                                        limit=query.get('limit', 50)))
    elif turn_id is None:
        _query(handler, set())
        value = handler._body()
        if access is not None:
            request = validate_turn(value)
            if request['mode'] == 'execute':
                from repo_agent.backend.routers.project_http import send_request
                send_request(handler, access.request_chat(handler.identity, chat, conversation, request))
                return
            if conversation['workspaceRef'] is not None:
                access.check_source(conversation['workspaceRef'])
        if access is None:
            status, record = chat.submit(conversation_id, value)
        else:
            status, record = chat.submit(conversation_id, value, authorization={
                'requesterId': handler.identity.account_id, 'projectId': conversation['workspaceRef']})
        handler._json(status, record, {'Location': record['statusUrl']})
    elif artifact_id:
        if access is not None and artifact_id != 'reply':
            from repo_agent.backend.services.project_access import admin_required
            admin_required(handler.identity)
        query = _query(handler, {'offset', 'limit'})
        handler._json(200, chat.artifact(conversation_id, turn_id, artifact_id, **query))
    elif events:
        query = _query(handler, {'after', 'limit', 'stream'})
        stream = query.pop('stream', 0)
        if stream not in (0, 1):
            raise TaskError(400, 'INVALID_PAGINATION', 'stream 须为 0 或 1')
        if stream:
            cursor = handler.headers.get('Last-Event-ID')
            after = query.get('after', 0)
            if cursor is not None:
                if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 19:
                    raise TaskError(400, 'INVALID_PAGINATION', '事件游标无效')
                after = max(after, int(cursor))
            _stream_events(handler, chat, conversation_id, turn_id, after)
        else:
            page = chat.events(conversation_id, turn_id, **query)
            handler._json(200, {**page, 'events': _project_events(handler, page['events'])})
    else:
        _query(handler, set())
        handler._json(200, chat.turn(conversation_id, turn_id))
