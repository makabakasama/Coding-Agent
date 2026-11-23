"""Transactional conversation records using the existing PostgreSQL pool."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import uuid

from repo_agent.backend.schemas.chat_contract import (
    ConversationConflict, validate_chat_id, validate_page, validate_sequence,
)
from repo_agent.backend.schemas.task_contract import TaskError, load_json
from repo_agent.infrastructure.file_io import now


class ChatStore:
    def __init__(self, store):
        self.store = store
        self.namespace = hashlib.sha256(str(store.root).encode()).hexdigest()
        with store._connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_conversations (
                conversation_id text PRIMARY KEY, namespace text NOT NULL,
                record jsonb NOT NULL, next_message_seq bigint NOT NULL DEFAULT 1,
                updated_at timestamptz NOT NULL)''')
            conn.execute('''CREATE INDEX IF NOT EXISTS chat_conversations_recent
                ON chat_conversations(namespace, updated_at DESC, conversation_id DESC)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_turns (
                turn_id text PRIMARY KEY,
                conversation_id text NOT NULL REFERENCES chat_conversations(conversation_id),
                client_request_id text NOT NULL, request_hash text NOT NULL, record jsonb NOT NULL,
                UNIQUE(conversation_id, client_request_id), UNIQUE(conversation_id, turn_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
                message_id text PRIMARY KEY, conversation_id text NOT NULL,
                turn_id text NOT NULL, seq bigint NOT NULL CHECK(seq > 0),
                role text NOT NULL CHECK(role IN ('user', 'assistant')), record jsonb NOT NULL,
                UNIQUE(conversation_id, seq), UNIQUE(turn_id, role),
                FOREIGN KEY(conversation_id, turn_id)
                    REFERENCES chat_turns(conversation_id, turn_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_turn_jobs (
                turn_id text PRIMARY KEY REFERENCES chat_turns(turn_id),
                namespace text NOT NULL, job_kind text NOT NULL CHECK(job_kind IN ('discuss', 'execute_bridge')),
                status text NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'unknown')),
                worker_id text, claim_id text, heartbeat_at timestamptz,
                queued_at timestamptz NOT NULL, started_at timestamptz, finished_at timestamptz,
                reason text)''')
            conn.execute('''CREATE INDEX IF NOT EXISTS chat_turn_jobs_pending
                ON chat_turn_jobs(namespace, status, queued_at, turn_id)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_turn_events (
                turn_id text NOT NULL REFERENCES chat_turns(turn_id),
                seq bigint NOT NULL CHECK(seq > 0), record jsonb NOT NULL,
                PRIMARY KEY(turn_id, seq))''')

    def _conversation(self, conn, conversation_id, *, lock=False):
        suffix = ' FOR UPDATE' if lock else ''
        row = conn.execute('''SELECT record, next_message_seq FROM chat_conversations
            WHERE conversation_id=%s AND namespace=%s''' + suffix,
                           (conversation_id, self.namespace)).fetchone()
        if row is None:
            raise TaskError(404, 'CONVERSATION_NOT_FOUND', '会话不存在')
        return row

    def create(self, value):
        identifier, stamp = uuid.uuid4().hex, now()
        record = {'schemaVersion': 1, 'conversationId': identifier,
                  'title': value['title'], 'workspaceRef': value['workspaceRef'],
                  'revision': 0, 'activeTurnId': None, 'latestTurnId': None,
                  'createdAt': stamp, 'updatedAt': stamp}
        with self.store._connection() as conn:
            conn.execute('''INSERT INTO chat_conversations
                (conversation_id, namespace, record, updated_at) VALUES (%s,%s,%s,%s)''',
                         (identifier, self.namespace, self.store._json(record), stamp))
        return record

    def get(self, conversation_id):
        validate_chat_id(conversation_id)
        with self.store._connection() as conn:
            record = self._conversation(conn, conversation_id)[0]
            if 'latestTurnId' not in record:
                row = conn.execute('''SELECT turn_id FROM chat_messages
                    WHERE conversation_id=%s AND role='user' ORDER BY seq DESC LIMIT 1''',
                                   (conversation_id,)).fetchone()
                record = {**record, 'latestTurnId': row[0] if row else None}
            return record

    def list(self, *, cursor=None, limit=50):
        validate_page(limit)
        condition, parameters = '', [self.namespace]
        if cursor is not None:
            try:
                if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 512
                        or not cursor.isascii()):
                    raise ValueError('Invalid cursor')
                decoded = base64.b64decode(cursor, altchars=b'-_', validate=True)
                position = load_json(decoded.decode('utf-8'))
                if not isinstance(position, list) or len(position) != 2:
                    raise ValueError('Invalid position')
                stamp, identifier = position
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace('Z', '+00:00')).tzinfo is None:
                    raise ValueError('Invalid timestamp')
                validate_chat_id(identifier)
            except (ValueError, UnicodeError, TaskError) as exc:
                raise TaskError(400, 'INVALID_PAGINATION', '会话列表游标无效') from exc
            condition = ' AND (updated_at, conversation_id) < (%s::timestamptz,%s)'
            parameters.extend((stamp, identifier))
        parameters.append(limit + 1)
        with self.store._connection() as conn:
            rows = conn.execute('''SELECT record FROM chat_conversations WHERE namespace=%s'''
                                + condition + ' ORDER BY updated_at DESC, conversation_id DESC LIMIT %s',
                                parameters).fetchall()
        records = [row[0] for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = records[-1]
            payload = json.dumps([last['updatedAt'], last['conversationId']]).encode()
            next_cursor = base64.urlsafe_b64encode(payload).decode('ascii')
        return {'conversations': records, 'hasMore': len(rows) > limit, 'nextCursor': next_cursor}

    def messages(self, conversation_id, *, after_seq=0, limit=50):
        validate_chat_id(conversation_id)
        validate_sequence(after_seq)
        validate_page(limit)
        with self.store._connection() as conn:
            self._conversation(conn, conversation_id)
            rows = conn.execute('''SELECT record FROM chat_messages
                WHERE conversation_id=%s AND seq>%s ORDER BY seq LIMIT %s''',
                                (conversation_id, after_seq, limit + 1)).fetchall()
        records = [row[0] for row in rows[:limit]]
        return {'messages': records, 'hasMore': len(rows) > limit,
                'nextAfterSeq': records[-1]['seq'] if records else after_seq}

    def submit(self, conversation_id, value):
        validate_chat_id(conversation_id)
        # Revision is a concurrency precondition, not part of the request's semantic identity.
        request = {key: item for key, item in value.items()
                   if key not in ('expectedRevision', 'frozenBaseCommit', '_clientRequestHash')}
        request_hash = value.get('_clientRequestHash') or hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        with self.store._connection() as conn:
            conversation, sequence = self._conversation(conn, conversation_id, lock=True)
            previous = conn.execute('''SELECT request_hash, record FROM chat_turns
                WHERE conversation_id=%s AND client_request_id=%s''',
                                    (conversation_id, value['clientRequestId'])).fetchone()
            if previous is not None:
                if previous[0] != request_hash:
                    raise ConversationConflict('REQUEST_KEY_REUSED', '同一请求编号已用于其他内容', conversation)
                return False, self._view_turn(conn, previous[1]), conversation
            if conversation['revision'] != value['expectedRevision']:
                raise ConversationConflict('REVISION_CONFLICT', '会话已更新，请读取最新状态', conversation)
            turn_id, message_id, stamp = uuid.uuid4().hex, uuid.uuid4().hex, now()
            execute = value['mode'] == 'execute'
            read_tools = ['list_files', 'search_code', 'read_file'] if conversation['workspaceRef'] else []
            policy = {'mode': value['mode'], 'allowWrites': execute,
                      'allowTests': value['execution']['allowTests'] if execute else False,
                      'allowedTools': read_tools, 'workspaceRef': conversation['workspaceRef']}
            if execute:
                policy['executionContract'] = value['execution']
                policy['frozenBaseCommit'] = value.get('frozenBaseCommit')
            turn = {'schemaVersion': 1, 'turnId': turn_id, 'conversationId': conversation_id,
                    'clientRequestId': value['clientRequestId'], 'mode': value['mode'],
                    'inputMessageId': message_id, 'contextCutoffSeq': sequence,
                    'policySnapshot': policy, 'status': 'queued', 'stage': 'queue',
                    'responseMessageId': None, 'stopReason': None, 'createdAt': stamp,
                    'startedAt': None, 'finishedAt': None}
            message = {'messageId': message_id, 'conversationId': conversation_id, 'turnId': turn_id,
                       'seq': sequence, 'role': 'user', 'content': value['content'],
                       'sourceRefs': [], 'createdAt': stamp}
            conn.execute('''INSERT INTO chat_turns
                (turn_id,conversation_id,client_request_id,request_hash,record) VALUES (%s,%s,%s,%s,%s)''',
                         (turn_id, conversation_id, value['clientRequestId'], request_hash, self.store._json(turn)))
            conn.execute('''INSERT INTO chat_messages
                (message_id,conversation_id,turn_id,seq,role,record) VALUES (%s,%s,%s,%s,'user',%s)''',
                         (message_id, conversation_id, turn_id, sequence, self.store._json(message)))
            conn.execute('''INSERT INTO chat_turn_jobs
                (turn_id,namespace,job_kind,status,queued_at) VALUES (%s,%s,%s,'queued',%s)''',
                         (turn_id, self.namespace, 'execute_bridge' if execute else 'discuss', stamp))
            event = {'seq': 1, 'at': stamp, 'type': 'turn_queued', 'turnId': turn_id}
            conn.execute('INSERT INTO chat_turn_events(turn_id,seq,record) VALUES (%s,1,%s)',
                         (turn_id, self.store._json(event)))
            conversation = {**conversation, 'revision': conversation['revision'] + 1,
                            'activeTurnId': conversation['activeTurnId'] or turn_id,
                            'latestTurnId': turn_id, 'updatedAt': stamp}
            conn.execute('''UPDATE chat_conversations SET record=%s, next_message_seq=%s, updated_at=%s
                WHERE conversation_id=%s AND namespace=%s''',
                         (self.store._json(conversation), sequence + 1, stamp, conversation_id, self.namespace))
        return True, turn, conversation

    def replay(self, conversation_id, request_id, request_hash):
        """Resolve a committed retry before changing project configuration is read."""
        validate_chat_id(conversation_id)
        with self.store._connection() as conn:
            conversation = self._conversation(conn, conversation_id)[0]
            row = conn.execute('''SELECT request_hash, record FROM chat_turns
                WHERE conversation_id=%s AND client_request_id=%s''',
                               (conversation_id, request_id)).fetchone()
            if row is None:
                return None
            if row[0] != request_hash:
                raise ConversationConflict('REQUEST_KEY_REUSED', '同一请求编号已用于其他内容', conversation)
            return self._view_turn(conn, row[1]), conversation

    def _turn(self, conn, conversation_id, turn_id):
        row = conn.execute('''SELECT t.record FROM chat_turns t
            JOIN chat_conversations c USING(conversation_id)
            WHERE t.conversation_id=%s AND t.turn_id=%s AND c.namespace=%s''',
                           (conversation_id, turn_id, self.namespace)).fetchone()
        if row is None:
            raise TaskError(404, 'TURN_NOT_FOUND', '轮次不存在')
        return row[0]

    def turn(self, conversation_id, turn_id):
        validate_chat_id(conversation_id)
        validate_chat_id(turn_id)
        with self.store._connection() as conn:
            return self._view_turn(conn, self._turn(conn, conversation_id, turn_id))

    def _view_turn(self, conn, turn):
        row = conn.execute('''SELECT status='running' AND
            (heartbeat_at IS NULL OR heartbeat_at < now()-interval '15 seconds')
            FROM chat_turn_jobs WHERE turn_id=%s AND namespace=%s''',
                           (turn['turnId'], self.namespace)).fetchone()
        if row and row[0]:
            return {**turn, 'status': 'unknown', 'stopReason': 'WORKER_HEARTBEAT_LOST'}
        return turn

    def _event(self, conn, turn_id, value):
        # All event writers hold the conversation row lock.
        sequence = conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM chat_turn_events WHERE turn_id=%s',
                                (turn_id,)).fetchone()[0]
        record = {**value, 'seq': sequence, 'turnId': turn_id, 'at': now()}
        conn.execute('INSERT INTO chat_turn_events(turn_id,seq,record) VALUES (%s,%s,%s)',
                     (turn_id, sequence, self.store._json(record)))
        return record

    def claim(self, worker_id):
        with self.store._connection() as conn:
            row = conn.execute('''SELECT c.conversation_id, j.turn_id
                FROM chat_turn_jobs j JOIN chat_turns t USING(turn_id)
                JOIN chat_conversations c USING(conversation_id)
                WHERE j.namespace=%s AND c.namespace=%s AND j.status='queued'
                AND j.job_kind IN ('discuss','execute_bridge') AND c.record->>'activeTurnId'=j.turn_id
                ORDER BY j.queued_at,j.turn_id FOR UPDATE OF c SKIP LOCKED LIMIT 1''',
                               (self.namespace, self.namespace)).fetchone()
            if row is None:
                return None
            conversation_id, turn_id = row
            claim_id, stamp = uuid.uuid4().hex, now()
            claimed = conn.execute('''UPDATE chat_turn_jobs SET status='running', worker_id=%s, claim_id=%s,
                started_at=%s, heartbeat_at=now() WHERE turn_id=%s AND status='queued'
                RETURNING turn_id''', (worker_id, claim_id, stamp, turn_id)).fetchone()
            if claimed is None:
                return None
            turn = self._turn(conn, conversation_id, turn_id)
            turn = {**turn, 'status': 'running',
                    'stage': 'bridge' if turn['mode'] == 'execute' else 'model', 'startedAt': stamp}
            conn.execute('UPDATE chat_turns SET record=%s WHERE turn_id=%s',
                         (self.store._json(turn), turn_id))
            self._event(conn, turn_id, {'type': 'turn_started'})
        return {'conversationId': conversation_id, 'turnId': turn_id,
                'workerId': worker_id, 'claimId': claim_id}

    def heartbeat(self, session, worker_id):
        session.execute('''UPDATE chat_turn_jobs SET heartbeat_at=now()
            WHERE namespace=%s AND worker_id=%s AND status='running' ''',
                        (self.namespace, worker_id))

    def _owned(self, conn, claim):
        conversation, sequence = self._conversation(conn, claim['conversationId'], lock=True)
        job = conn.execute('''SELECT status, worker_id, claim_id FROM chat_turn_jobs
            WHERE turn_id=%s AND namespace=%s FOR UPDATE''',
                           (claim['turnId'], self.namespace)).fetchone()
        if job is None or job[1:] != (claim['workerId'], claim['claimId']):
            raise TaskError(409, 'CHAT_CLAIM_LOST', '轮次领取身份已失效')
        turn = self._turn(conn, claim['conversationId'], claim['turnId'])
        return conversation, sequence, turn, job[0]

    def linked_task(self, claim, task_id):
        with self.store._connection() as conn:
            _, _, turn, status = self._owned(conn, claim)
            if turn['mode'] != 'execute' or status not in ('running', 'unknown') or task_id != claim['turnId']:
                raise TaskError(409, 'CHAT_CLAIM_LOST', '执行关联身份无效')
            if turn.get('taskId') not in (None, task_id):
                raise TaskError(409, 'TASK_ID_REUSED', '会话轮次已关联其他任务')
            turn = {**turn, 'taskId': task_id, 'stage': 'preparing'}
            conn.execute('UPDATE chat_turns SET record=%s WHERE turn_id=%s',
                         (self.store._json(turn), claim['turnId']))
            self._event(conn, claim['turnId'], {'type': 'task_linked', 'taskId': task_id})

    def bridge_started(self, claim, task_id, execution_id):
        with self.store._connection() as conn:
            _, _, turn, status = self._owned(conn, claim)
            if (turn['mode'] != 'execute' or status not in ('running', 'unknown')
                    or turn.get('taskId') != task_id):
                raise TaskError(409, 'CHAT_CLAIM_LOST', '执行关联身份无效')
            turn = {**turn, 'status': 'running', 'stage': 'execution',
                    'taskId': task_id, 'executionId': execution_id}
            conn.execute('UPDATE chat_turns SET record=%s WHERE turn_id=%s',
                         (self.store._json(turn), claim['turnId']))
            conn.execute('''UPDATE chat_turn_jobs SET status='completed', reason='BRIDGED',
                finished_at=now() WHERE turn_id=%s AND namespace=%s AND worker_id=%s AND claim_id=%s''',
                         (claim['turnId'], self.namespace, claim['workerId'], claim['claimId']))
            self._event(conn, claim['turnId'], {'type': 'task_started',
                                               'taskId': task_id, 'executionId': execution_id})

    def executing(self):
        with self.store._connection() as conn:
            rows = conn.execute('''SELECT t.conversation_id,j.turn_id,j.worker_id,j.claim_id,
                t.record->>'taskId',t.record->>'executionId'
                FROM chat_turn_jobs j JOIN chat_turns t USING(turn_id)
                WHERE j.namespace=%s AND j.job_kind='execute_bridge' AND j.status='completed'
                  AND t.record->>'status' IN ('running','unknown')
                  AND t.record->>'stage'='execution'
                ORDER BY j.queued_at,j.turn_id''', (self.namespace,)).fetchall()
        return [dict(zip(('conversationId', 'turnId', 'workerId', 'claimId',
                          'taskId', 'executionId'), row)) for row in rows]

    def execution_unknown(self, claim, reason):
        with self.store._connection() as conn:
            _, _, turn, status = self._owned(conn, claim)
            if (status != 'completed' or turn['mode'] != 'execute'
                    or turn.get('taskId') != claim['taskId'] or turn['status'] != 'running'):
                return
            turn = {**turn, 'status': 'unknown', 'stopReason': reason}
            conn.execute('UPDATE chat_turns SET record=%s WHERE turn_id=%s',
                         (self.store._json(turn), claim['turnId']))
            self._event(conn, claim['turnId'], {'type': 'execution_unknown', 'reason': reason,
                                               'taskId': claim['taskId']})

    def context(self, claim, *, max_input_bytes):
        with self.store._connection() as conn:
            _, _, turn, status = self._owned(conn, claim)
            if status != 'running' and not (turn['mode'] == 'execute' and status == 'unknown'):
                raise TaskError(409, 'CHAT_CLAIM_LOST', '轮次已结束')
            # Messages submitted during an earlier run have smaller arrival seq
            # than that run's answer. Assemble by turn, so queued user text is
            # never placed before the answer it waited for.
            rows = conn.execute('''SELECT m.record, t.record->>'status', t.record->>'stopReason',
                u.seq, t.record->'executionEvidence', t.record->'artifacts', t.record->'policySnapshot'
                FROM chat_messages u JOIN chat_messages m
                  ON m.turn_id=u.turn_id AND m.conversation_id=u.conversation_id
                JOIN chat_turns t ON t.turn_id=u.turn_id
                WHERE u.conversation_id=%s AND u.role='user' AND u.seq<=%s
                ORDER BY u.seq, CASE m.role WHEN 'user' THEN 0 ELSE 1 END''',
                                (claim['conversationId'], turn['contextCutoffSeq'])).fetchall()
            size = sum(len(row[0]['content'].encode('utf-8')) for row in rows)
            if size > max_input_bytes:
                raise TaskError(400, 'CONTEXT_LIMIT', '会话历史超过输入上限，请缩小上下文或新建会话')
        return turn, [{'message': row[0], 'turnStatus': row[1], 'stopReason': row[2],
                       'turnInputSeq': row[3], 'executionEvidence': row[4],
                       'artifacts': row[5], 'policySnapshot': row[6]} for row in rows]

    def claimed_turn(self, claim):
        with self.store._connection() as conn:
            _, _, turn, _ = self._owned(conn, claim)
            return turn

    def record_event(self, claim, value):
        with self.store._connection() as conn:
            _, _, _, status = self._owned(conn, claim)
            if status != 'running':
                raise TaskError(409, 'CHAT_CLAIM_LOST', '轮次已结束')
            return self._event(conn, claim['turnId'], value)

    def finish(self, claim, result, *, recovered=False):
        status = result['status']
        if status not in ('completed', 'failed'):
            raise ValueError('A chat result must be completed or failed')
        with self.store._connection() as conn:
            conversation, sequence, turn, current = self._owned(conn, claim)
            if turn['status'] in ('completed', 'failed'):
                return turn
            bridged = turn['mode'] == 'execute' and current == 'completed' and turn.get('stage') == 'execution'
            if (current not in ('running', 'unknown') and not bridged
                    or conversation['activeTurnId'] != claim['turnId']):
                raise TaskError(409, 'CHAT_CLAIM_LOST', '轮次已不再活动')
            stamp, response_id = now(), None
            if status == 'completed':
                text = result.get('text')
                if not isinstance(text, str) or not text.strip() or '\x00' in text:
                    raise ValueError('A completed chat result requires valid text')
                text.encode('utf-8')
                response_id = uuid.uuid4().hex
                message = {'messageId': response_id, 'conversationId': claim['conversationId'],
                           'turnId': claim['turnId'], 'seq': sequence, 'role': 'assistant',
                           'content': text, 'sourceRefs': list(result.get('artifacts', {}).values()),
                           'createdAt': stamp}
                conn.execute('''INSERT INTO chat_messages
                    (message_id,conversation_id,turn_id,seq,role,record) VALUES (%s,%s,%s,%s,'assistant',%s)''',
                             (response_id, claim['conversationId'], claim['turnId'], sequence,
                              self.store._json(message)))
                sequence += 1
            turn = {**turn, 'status': status, 'stage': 'finished', 'finishedAt': stamp,
                    'responseMessageId': response_id, 'stopReason': result['stopReason'],
                    'artifacts': result.get('artifacts', {}), 'metrics': result.get('metrics', {}),
                    'error': result.get('error'), 'recovered': recovered,
                    'executionEvidence': result.get('executionEvidence')}
            conn.execute('UPDATE chat_turns SET record=%s WHERE turn_id=%s',
                         (self.store._json(turn), claim['turnId']))
            conn.execute('''UPDATE chat_turn_jobs SET status=%s, reason=%s, finished_at=%s
                WHERE turn_id=%s AND namespace=%s AND worker_id=%s AND claim_id=%s''',
                         (status, result['stopReason'], stamp, claim['turnId'], self.namespace,
                          claim['workerId'], claim['claimId']))
            next_row = conn.execute('''SELECT j.turn_id FROM chat_turn_jobs j
                JOIN chat_messages u ON u.turn_id=j.turn_id AND u.role='user'
                WHERE u.conversation_id=%s AND j.namespace=%s AND j.status='queued'
                ORDER BY u.seq LIMIT 1''',
                                    (claim['conversationId'], self.namespace)).fetchone()
            conversation = {**conversation, 'activeTurnId': next_row[0] if next_row else None,
                            'latestTurnId': conversation.get('latestTurnId') or claim['turnId'],
                            'revision': conversation['revision'] + 1, 'updatedAt': stamp}
            conn.execute('''UPDATE chat_conversations SET record=%s, next_message_seq=%s, updated_at=%s
                WHERE conversation_id=%s AND namespace=%s''',
                         (self.store._json(conversation), sequence, stamp,
                          claim['conversationId'], self.namespace))
            self._event(conn, claim['turnId'], {'type': 'turn_finished', 'status': status,
                                               'stopReason': result['stopReason'], 'recovered': recovered})
        return turn

    def interrupted(self):
        """Call only after acquiring the pool session and process-scoped file lock."""
        with self.store._connection() as conn:
            rows = conn.execute('''SELECT t.conversation_id, j.turn_id, j.worker_id, j.claim_id
                FROM chat_turn_jobs j JOIN chat_turns t USING(turn_id)
                WHERE j.namespace=%s AND j.status IN ('running','unknown')
                ORDER BY j.queued_at,j.turn_id''', (self.namespace,)).fetchall()
        return [dict(zip(('conversationId', 'turnId', 'workerId', 'claimId'), row)) for row in rows]

    def events(self, conversation_id, turn_id, *, after=0, limit=50):
        validate_chat_id(conversation_id)
        validate_chat_id(turn_id)
        validate_sequence(after)
        validate_page(limit)
        with self.store._connection() as conn:
            self._turn(conn, conversation_id, turn_id)
            rows = conn.execute('''SELECT record FROM chat_turn_events
                WHERE turn_id=%s AND seq>%s ORDER BY seq LIMIT %s''',
                                (turn_id, after, limit + 1)).fetchall()
        records = [row[0] for row in rows[:limit]]
        return {'events': records, 'hasMore': len(rows) > limit,
                'nextAfter': records[-1]['seq'] if records else after}
