"""HTTP routes and resource-based guards for project authorization."""
import re
from urllib.parse import parse_qs, urlsplit

from repo_agent.backend.repositories.project_access_store import ROLES
from repo_agent.backend.schemas.chat_contract import validate_chat_id
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.project_access import admin_required


PROJECT_ENDPOINT = re.compile(r'/api/projects/([^/]+)/(enable|members|execution-requests)(?:/([0-9a-f]{32}))?')
AUDIT_ENDPOINT = re.compile(r'/api/projects/([^/]+)/executions/([0-9a-f]{32})/audit')
PROJECT_TASK = re.compile(r'/api/projects/([^/]+)/tasks(?:/[^/]+(?:/(?:discussion|runs(?:/status)?|rounds'
                          r'(?:/[^/]+(?:/(?:verifications|artifacts/[^/]+))?)?))?)?')
TASK_ENDPOINT = re.compile(r'/api/tasks/([^/]+)(?:/(.*))?')
PUBLIC_TASK_READS = {None, 'organization', 'review', 'preparation', 'execution',
                     'execution/events', 'execution/result'}


def dispatch_projects(handler, path):
    audit_match = AUDIT_ENDPOINT.fullmatch(path)
    if audit_match:
        project, execution = audit_match.groups()
        handler.access.require(handler.identity, project)
        if handler.command != 'GET':
            raise TaskError(405, 'METHOD_NOT_ALLOWED', '审计记录仅支持读取')
        handler._json(200, handler.access.audit.history(project, execution))
        return True
    matched = PROJECT_ENDPOINT.fullmatch(path)
    if matched is None:
        return False
    project, endpoint, request_id = matched.groups()
    access, identity, method = handler.access, handler.identity, handler.command
    if request_id is not None and endpoint != 'execution-requests':
        return False
    if endpoint == 'enable' and method == 'POST':
        admin_required(identity)
        access.enable(identity, project, handler._body())
        handler._json(201, {'projectId': project, 'enabled': True})
    elif endpoint == 'members' and method in ('GET', 'POST'):
        access.require(identity, project, 'maintainer')
        if method == 'POST':
            access.edit_member(identity, project, handler._body())
            handler._json(200, {'changed': True})
        else:
            handler._json(200, {'members': access.repository.members(project)})
    elif endpoint == 'execution-requests':
        access.require(identity, project)
        if request_id is None and method == 'GET':
            with access.store._connection() as conn:
                access.repository.project(conn, project)
            query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
            state = query.get('status', ['pending'])
            if set(query) - {'status'} or len(state) != 1 or state[0] not in ('pending', 'approved', 'rejected'):
                raise TaskError(400, 'INVALID_REQUEST_STATUS', '执行申请状态无效')
            handler._json(200, {'requests': access.repository.requests(project, state[0])})
        elif request_id is not None and method == 'GET':
            with access.store._connection() as conn:
                record = access.repository.request(conn, project, validate_chat_id(request_id))
            handler._json(200, record)
        elif request_id is not None and method == 'POST':
            admin_required(identity)
            handler._json(200, access.decide(identity, project, request_id, handler._body()))
        else:
            raise TaskError(405, 'METHOD_NOT_ALLOWED', '申请接口不支持此方法')
    else:
        raise TaskError(405, 'METHOD_NOT_ALLOWED', '项目管理接口不支持此方法')
    return True


def guard_business(handler, path):
    access, identity, method = handler.access, handler.identity, handler.command
    if path in ('/api/projects', '/api/tasks'):
        return
    matched = PROJECT_TASK.fullmatch(path)
    if matched:
        project = matched[1]
        access.require(identity, project, 'developer' if method == 'POST' else 'observer')
        if path.endswith('/runs') and method == 'POST':
            admin_required(identity)
        return
    matched = TASK_ENDPOINT.fullmatch(path)
    if matched:
        task_id, operation = matched.groups()
        access.require_task(identity, task_id)
        if method == 'GET' and (operation in PUBLIC_TASK_READS
                or operation and re.fullmatch(r'execution/artifacts/[^/]+', operation)):
            return
        if method == 'POST' and operation in ('execution', 'review'):
            access.require_task(identity, task_id, 'developer' if operation == 'execution' else 'maintainer')
            return
    admin_required(identity)


def visible_projects(handler, projects):
    roles = handler.access.roles(handler.identity)
    enabled = (handler.access.repository.enabled_projects() if roles is None else roles.keys())
    result = []
    for project in projects:
        identifier = project['projectId']
        if roles is not None and identifier not in roles:
            continue
        role = 'admin' if roles is None else roles[identifier]
        create = role == 'admin' or ROLES[role] >= ROLES['developer']
        result.append({**project, 'role': role, 'accessEnabled': identifier in enabled,
                       'canCreateTask': create and project['canCreateTask'],
                       'canStartExperiment': role == 'admin' and project['canStartExperiment']})
    return result


def send_request(handler, request):
    location = f'/api/projects/{request["projectId"]}/execution-requests/{request["requestId"]}'
    handler._json(202, {'executionRequest': request, 'statusUrl': location}, {'Location': location})
