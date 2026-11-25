"""Durable execution identity, authorization checks, and append-only audit."""
from hashlib import sha256
import json
from pathlib import Path

from repo_agent.backend.repositories.project_access_store import ROLES
from repo_agent.backend.schemas.task_contract import TaskError


def fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return sha256(data).hexdigest()


def approval_proof(request, approver):
    return {'requestId': request['requestId'], 'projectId': request['projectId'],
            'requesterId': request['requesterId'], 'approverId': approver,
            'approvalSha256': fingerprint(request['snapshot'])}


def task_scope(contract, task_key, review):
    return {'contract': contract, 'taskKey': task_key, 'reviewBase': review}


def chat_policy_scope(turn):
    policy = turn.get('policySnapshot')
    if not isinstance(policy, dict):
        return None
    return {key: policy.get(key) for key in ('executionContract', 'requirementKey',
                                            'reviewBase', 'frozenBaseCommit')}


def approved_chat_scope(snapshot):
    return {'executionContract': snapshot['execution'], 'requirementKey': snapshot.get('requirementKey'),
            'reviewBase': snapshot.get('reviewBase'), 'frozenBaseCommit': snapshot.get('frozenBaseCommit')}


def matches_bridge_task(contract, key, review, snapshot):
    actual = {name: value for name, value in contract.items()
              if name not in ('baseCommit', 'requestedRevision', 'conversationContext')}
    actual['revision'] = contract.get('requestedRevision')
    expected = dict(snapshot['execution'])
    expected['revision'] = snapshot.get('frozenBaseCommit') or expected['revision']
    expected_key = (snapshot.get('reviewBase') or {}).get('taskKey') or snapshot.get('requirementKey')
    return (actual == expected and contract.get('baseCommit') == snapshot.get('frozenBaseCommit')
            and review == snapshot.get('reviewBase') and (expected_key is None or key == expected_key))


class ExecutionAudit:
    def __init__(self, store, projects):
        self.store, self.projects = store, projects
        self.namespace = sha256(str(store.root).encode()).hexdigest()
        with store._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_account_namespaces (
                namespace text PRIMARY KEY
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_execution_audit (
                event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                namespace text NOT NULL, event_key text NOT NULL, event_type text NOT NULL,
                request_id text, task_id text, execution_id text, project_id text,
                account_id text, worker_id text, reason text, artifact_sha256 text,
                at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (namespace, event_key)
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS workbench_execution_audit_lookup
                ON workbench_execution_audit(namespace, execution_id, event_id)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS workbench_execution_audit_request
                ON workbench_execution_audit(namespace, request_id, event_id)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workbench_execution_reviews (
                namespace text NOT NULL, task_id text NOT NULL, execution_id text NOT NULL,
                revision bigint NOT NULL, record jsonb NOT NULL,
                PRIMARY KEY (namespace, task_id)
            )""")

    def lock(self, conn):
        # Acquire before project, conversation, and dispatch locks.
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 2))', (self.namespace,))

    def enable(self):
        with self.store._connection() as conn:
            self.lock(conn)
            conn.execute('INSERT INTO workbench_account_namespaces VALUES (%s) ON CONFLICT DO NOTHING',
                         (self.namespace,))

    def required(self, conn):
        return conn.execute('SELECT 1 FROM workbench_account_namespaces WHERE namespace=%s',
                            (self.namespace,)).fetchone() is not None

    def account_mode(self):
        with self.store._connection() as conn:
            return self.required(conn)

    def event(self, conn, key, kind, *, request=None, task=None, execution=None, project=None,
              actor=None, worker=None, reason=None, artifact_sha=None):
        conn.execute("""INSERT INTO workbench_execution_audit
            (namespace,event_key,event_type,request_id,task_id,execution_id,project_id,account_id,worker_id,reason,
             artifact_sha256)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (namespace,event_key) DO NOTHING""",
                     (self.namespace, key, kind, request, task, execution, project, actor, worker, reason, artifact_sha))

    def job_event(self, conn, kind, task, execution, proof, worker=None, reason=None, *, stage='task'):
        self.event(conn, f'{stage}:{execution or task}:{kind}', kind,
                   request=proof.get('requestId'), task=task, execution=execution,
                   project=proof.get('projectId'), worker=worker, reason=reason)

    @staticmethod
    def account(conn, account_id):
        return conn.execute("""SELECT enabled, admin FROM workbench_accounts
            WHERE account_id=%s FOR SHARE""", (account_id,)).fetchone()

    def member(self, conn, project, account_id, role):
        row = conn.execute("""SELECT role FROM workbench_project_members
            WHERE namespace=%s AND project_id=%s AND account_id=%s""",
                           (self.namespace, project, account_id)).fetchone()
        return row is not None and ROLES[row[0]] >= ROLES[role]

    def source(self, conn, project):
        row = conn.execute("""SELECT source_path FROM workbench_projects
            WHERE namespace=%s AND project_id=%s""", (self.namespace, project)).fetchone()
        if row is None:
            return None
        entry = self.projects._projects().get(project)
        if entry is None:
            return None
        try:
            path = (self.projects.config_path.parent / Path(entry['repo'])).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        return str(path) if path == Path(row[0]) else None

    def authorize_approval(self, conn, proof, task, execution, *, kind):
        if not isinstance(proof, dict) or not proof.get('requestId'):
            return 'AUTH_APPROVAL_REQUIRED', None
        row = conn.execute("""SELECT project_id, requester_id, decided_by, kind, target_id, snapshot, execution
            FROM workbench_execution_requests WHERE namespace=%s AND request_id=%s AND status='approved'""",
                           (self.namespace, proof['requestId'])).fetchone()
        if row is None:
            return 'AUTH_APPROVAL_REQUIRED', None
        project, requester, approver, stored_kind, target, snapshot, binding = row
        if (not isinstance(binding, dict) or stored_kind != kind
                or binding.get('taskId') != task or binding.get('executionId') != execution
                or (kind == 'task' and target != task)
                or (kind == 'chat' and (binding.get('turnId') != task or binding.get('conversationId') != target))
                or (project, requester, approver, fingerprint(snapshot)) != (
                    proof.get('projectId'), proof.get('requesterId'), proof.get('approverId'),
                    proof.get('approvalSha256'))):
            return 'AUTH_SCOPE_MISMATCH', None
        requester_row = self.account(conn, requester)
        if not requester_row or not requester_row[0]:
            return 'AUTH_REQUESTER_DISABLED', None
        approver_row = self.account(conn, approver)
        if not approver_row or not all(approver_row):
            return 'AUTH_APPROVER_REVOKED', None
        if not requester_row[1] and not self.member(conn, project, requester, 'developer'):
            return 'AUTH_REQUESTER_PERMISSION_REVOKED', None
        source = self.source(conn, project)
        if source is None:
            return 'AUTH_PROJECT_SOURCE_CHANGED', None
        return None, (source, snapshot, binding)

    def authorize_task(self, conn, task, execution, repository_key, workspace_mode, proof):
        kind = proof.get('kind') if isinstance(proof, dict) else None
        if kind not in ('task', 'chat'):
            return 'AUTH_APPROVAL_REQUIRED'
        reason, approved = self.authorize_approval(conn, proof, task, execution, kind=kind)
        if reason:
            return reason
        source, snapshot, _ = approved
        row = conn.execute("""SELECT t.project_id, t.task_key, r.record->'contract', r.metadata->'reviewBase'
            FROM task_runs r JOIN tasks t USING(task_key) WHERE r.task_id=%s""", (task,)).fetchone()
        if row:
            project, key, contract, review = row
            current = task_scope(contract, key, review)
            if (isinstance(contract, dict) and project == proof['projectId'] == contract.get('projectId')
                    and fingerprint(current) == proof.get('scopeSha256')
                    and (current == snapshot if kind == 'task' else matches_bridge_task(contract, key, review, snapshot))
                    and workspace_mode == contract.get('workspaceMode', 'worktree')
                    and repository_key == sha256(source.casefold().encode()).hexdigest()):
                return None
        return 'AUTH_SCOPE_MISMATCH'

    def authorize_chat(self, conn, conversation, turn, job_kind, proof, execution):
        if not isinstance(proof, dict):
            return 'AUTH_APPROVAL_REQUIRED' if job_kind == 'execute_bridge' else 'AUTH_DISCUSSION_ACTOR_REQUIRED'
        project = conversation.get('workspaceRef')
        if project != proof.get('projectId') or conversation['conversationId'] != turn['conversationId']:
            return 'AUTH_SCOPE_MISMATCH'
        if job_kind == 'execute_bridge':
            current = chat_policy_scope(turn)
            if turn['mode'] != 'execute' or current is None or fingerprint(current) != proof.get('scopeSha256'):
                return 'AUTH_SCOPE_MISMATCH'
            reason, approved = self.authorize_approval(conn, proof, turn['turnId'], execution, kind='chat')
            if reason:
                return reason
            _, snapshot, binding = approved
            if current != approved_chat_scope(snapshot) or binding['conversationId'] != conversation['conversationId']:
                return 'AUTH_SCOPE_MISMATCH'
            return None
        if turn['mode'] != 'discuss':
            return 'AUTH_SCOPE_MISMATCH'
        actor = proof.get('requesterId')
        row = self.account(conn, actor)
        if not row or not row[0]:
            return 'AUTH_REQUESTER_DISABLED'
        if project is None:
            owner = conversation.get('ownerAccountId')
            return None if actor == owner or (owner is None and row[1]) else 'AUTH_REQUESTER_PERMISSION_REVOKED'
        if not row[1] and not self.member(conn, project, actor, 'developer'):
            return 'AUTH_REQUESTER_PERMISSION_REVOKED'
        return None if self.source(conn, project) else 'AUTH_PROJECT_SOURCE_CHANGED'

    def bind_bridge_task(self, task, proof, execution):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT snapshot, execution FROM workbench_execution_requests
                WHERE namespace=%s AND request_id=%s AND status='approved' AND kind='chat'""",
                               (self.namespace, proof['requestId'])).fetchone()
            material = conn.execute("""SELECT t.task_key, r.metadata->'reviewBase' FROM task_runs r
                JOIN tasks t USING(task_key) WHERE r.task_id=%s""", (task['taskId'],)).fetchone()
        if row is not None and material is not None:
            snapshot, binding = row
            key, review = material
            contract = task['contract']
            if (isinstance(binding, dict) and binding.get('taskId') == task['taskId']
                    and binding.get('executionId') == execution
                    and fingerprint(snapshot) == proof['approvalSha256']
                    and matches_bridge_task(contract, key, review, snapshot)):
                return {**proof, 'scopeSha256': fingerprint(task_scope(contract, key, review))}
        raise TaskError(409, 'AUTH_SCOPE_MISMATCH', '编码任务与已批准会话范围不一致')

    def bridge_dispatch(self, claim):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT d.execution_id FROM chat_turn_jobs j
                JOIN task_dispatch d ON d.task_id=j.turn_id AND d.namespace=j.namespace
                WHERE j.namespace=%s AND j.turn_id=%s AND j.claim_id=%s AND j.worker_id=%s
                AND j.authorized_execution_id=d.execution_id
                AND j.authorization_proof->>'requestId'=d.authorization_proof->>'requestId'""",
                               (self.namespace, claim['turnId'], claim['claimId'], claim['workerId'])).fetchone()
        return row[0] if row else None

    def review(self, task):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT record FROM workbench_execution_reviews
                WHERE namespace=%s AND task_id=%s""", (self.namespace, task)).fetchone()
        return row[0] if row else None

    def save_review(self, task, execution, actor, decision, patch_sha):
        from repo_agent.infrastructure.file_io import now

        with self.store._connection() as conn:
            self.lock(conn)
            row = conn.execute("""SELECT t.project_id,d.authorization_proof FROM task_dispatch d
                JOIN task_runs r USING(task_id) JOIN tasks t USING(task_key)
                WHERE d.namespace=%s AND d.task_id=%s AND d.execution_id=%s
                    AND d.status='terminated' FOR SHARE OF d""",
                               (self.namespace, task, execution)).fetchone()
            if row is None:
                raise TaskError(409, 'REVIEW_VERSION_CHANGED', '无法定位已终止的执行记录')
            project, proof = row
            account = self.account(conn, actor.account_id)
            if not account or not account[0] or (not account[1]
                    and not self.member(conn, project, actor.account_id, 'maintainer')):
                raise TaskError(403, 'ROLE_REQUIRED', '当前账号没有审查权限')
            prior = conn.execute("""SELECT revision,record FROM workbench_execution_reviews
                WHERE namespace=%s AND task_id=%s FOR UPDATE""", (self.namespace, task)).fetchone()
            if prior and prior[1]['executionId'] == execution and prior[1]['decision'] == decision:
                return prior[1]
            revision = prior[0] + 1 if prior else 1
            record = {'taskId': task, 'executionId': execution, 'decision': decision,
                      'accountId': actor.account_id, 'at': now(), 'patchSha256': patch_sha}
            conn.execute("""INSERT INTO workbench_execution_reviews VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (namespace,task_id) DO UPDATE SET execution_id=EXCLUDED.execution_id,
                    revision=EXCLUDED.revision, record=EXCLUDED.record""",
                         (self.namespace, task, execution, revision, self.store._json(record)))
            self.event(conn, f'review:{execution}:{revision}', f'review_{decision}',
                       request=(proof or {}).get('requestId'), task=task, execution=execution,
                       project=project, actor=actor.account_id, artifact_sha=patch_sha)
            return record

    def history(self, project, execution):
        with self.store._connection() as conn:
            binding = conn.execute("""SELECT request_id,task_id FROM workbench_execution_audit
                WHERE namespace=%s AND project_id=%s AND execution_id=%s
                ORDER BY event_id DESC LIMIT 1""", (self.namespace, project, execution)).fetchone()
            if binding is None:
                raise TaskError(404, 'EXECUTION_AUDIT_NOT_FOUND', '暂无该执行的审计记录')
            request, task = binding
            rows = conn.execute("""SELECT v.event_type,v.request_id,v.task_id,v.execution_id,v.project_id,v.account_id,
                a.login,v.worker_id,v.reason,v.artifact_sha256,v.at FROM workbench_execution_audit v
                LEFT JOIN workbench_accounts a ON a.account_id=v.account_id
                WHERE v.namespace=%s AND v.project_id=%s AND (v.execution_id=%s OR v.request_id=%s)
                ORDER BY v.event_id""",
                               (self.namespace, project, execution, request)).fetchall()
        names = ('type', 'requestId', 'taskId', 'executionId', 'projectId', 'accountId', 'login', 'workerId',
                 'reason', 'artifactSha256')
        return {'taskId': task, 'executionId': execution,
                'events': [{**dict(zip(names, row)), 'at': row[-1].isoformat()} for row in rows]}
