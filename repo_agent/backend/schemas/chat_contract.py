"""Validation for persistent conversations and discussion submissions."""
from __future__ import annotations

import re

from repo_agent.backend.schemas.task_contract import PROJECT_ID, TaskError


CHAT_ID = re.compile(r"[0-9a-f]{32}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_MESSAGE_CHARS = 8000


class ConversationConflict(TaskError):
    def __init__(self, code, message, conversation):
        super().__init__(409, code, message)
        self.current_revision = conversation['revision']
        self.active_turn_id = conversation['activeTurnId']

    def response(self):
        result = super().response()
        result['error'].update(currentRevision=self.current_revision,
                               activeTurnId=self.active_turn_id)
        return result


def validate_chat_id(value):
    if not isinstance(value, str) or not CHAT_ID.fullmatch(value):
        raise TaskError(400, 'INVALID_CHAT_ID', '会话或轮次编号格式不正确')
    return value


def _text(value, field, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any((ord(char) < 32 and char not in '\n\r\t')
                   or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in value)):
        raise TaskError(400, 'VALIDATION_ERROR', '文本为空、过长或包含无效字符',
                        [{'field': field, 'message': f'须为 1 到 {maximum} 字符的有效文本'}])
    return value


def validate_conversation(value):
    if type(value) is not dict or set(value) - {'title', 'workspaceRef'}:
        raise TaskError(400, 'VALIDATION_ERROR', '会话只接受 title 和 workspaceRef')
    title = _text(value.get('title', '新会话'), 'title', 120).strip()
    workspace = value.get('workspaceRef')
    if workspace is not None and (not isinstance(workspace, str) or not PROJECT_ID.fullmatch(workspace)):
        raise TaskError(400, 'VALIDATION_ERROR', 'workspaceRef 须为已登记的项目编号')
    return {'title': title, 'workspaceRef': workspace}


def validate_turn(value):
    required = {'schemaVersion', 'clientRequestId', 'expectedRevision', 'content'}
    if type(value) is not dict or not required <= value.keys() or set(value) - (required | {'mode', 'execution'}):
        raise TaskError(400, 'VALIDATION_ERROR', '轮次字段缺失或包含未支持的字段')
    if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise TaskError(400, 'VALIDATION_ERROR', '轮次 schemaVersion 须为 1')
    request_id = value['clientRequestId']
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise TaskError(400, 'VALIDATION_ERROR', 'clientRequestId 须为 1 到 128 位字母、数字、下划线或连字符')
    revision = value['expectedRevision']
    if type(revision) is not int or not 0 <= revision <= 9223372036854775807:
        raise TaskError(400, 'VALIDATION_ERROR', 'expectedRevision 须为非负整数')
    mode = value.get('mode', 'discuss')
    if mode not in ('discuss', 'execute'):
        raise TaskError(400, 'CHAT_MODE_UNSUPPORTED', '轮次模式只支持 discuss 或 execute')
    if (mode == 'discuss' and 'execution' in value
            or mode == 'execute' and type(value.get('execution')) is not dict):
        raise TaskError(400, 'VALIDATION_ERROR', '执行轮须提供执行设置，讨论轮不得包含执行设置')
    result = {'schemaVersion': 1, 'clientRequestId': request_id,
            'expectedRevision': revision, 'mode': mode,
            'content': _text(value['content'], 'content', MAX_MESSAGE_CHARS)}
    if mode == 'execute':
        result['execution'] = value['execution']
    return result


def validate_page(value, *, maximum=100):
    if type(value) is not int or not 1 <= value <= maximum:
        raise TaskError(400, 'INVALID_PAGINATION', f'分页大小须为 1 到 {maximum} 的整数')
    return value


def validate_sequence(value):
    if type(value) is not int or not 0 <= value <= 9223372036854775807:
        raise TaskError(400, 'INVALID_PAGINATION', '游标须为非负整数')
    return value
