"""Focused Chat acceptance. Scripted models, loopback HTTP, dedicated PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
from uuid import uuid4

from repo_agent.backend.app import create_server
from repo_agent.backend.schemas.chat_contract import validate_turn
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.chat_execution import ChatExecution
from repo_agent.backend.services.worker_pool import WorkerPool
from repo_agent.core.decisions import Decision
from repo_agent.core.context import latest_tool_result
from repo_agent.core.provider import ContextLimitError, OpenAICompatibleProvider, ProviderError, TextReply, ToolCallReply
from tests.chat_fixture import FixtureChatModel, build_fixture


def submission(content='第一轮问题', revision=0, request_id='request-1'):
    return {'schemaVersion': 1, 'clientRequestId': request_id, 'expectedRevision': revision,
            'mode': 'discuss', 'content': content}


class ChatProviderAcceptance(unittest.TestCase):
    def test_coding_delivery_preserves_pending_acceptance(self):
        service = Mock()
        service.store.executing.return_value = [{'taskId': 'task', 'executionId': 'execution'}]
        service.task_service.execution.get.return_value = {'status': 'terminated', 'resultAvailable': True}
        gap = {'scenario': 'Independent branches', 'verification_goal': 'Preserve each branch order'}
        service.task_service.execution.result.return_value = {
            'completion': {'status': 'completed'}, 'execution_status': 'agent_reported_finished',
            'acceptance_status': 'needs_review', 'priority_gap': gap, 'remaining_gaps': [gap]}

        ChatExecution.reconcile(service)

        result = service.store.finish.call_args.args[1]
        self.assertEqual('needs_review', result['executionEvidence']['acceptance_status'])
        self.assertEqual([gap], result['executionEvidence']['remaining_gaps'])
        self.assertIn('模型报告：completed', result['text'])
        self.assertIn(gap['verification_goal'], result['text'])

    def provider(self):
        with patch('repo_agent.core.provider.read_dotenv', return_value={}):
            return OpenAICompatibleProvider(model='offline-test', base_url='https://model.invalid/v1', api_key='')

    def test_text_request_evidence_limits_and_usage(self):
        provider, events = self.provider(), []
        provider.set_event_sink(events.append)
        response = {'choices': [{'message': {'role': 'assistant', 'content': '你好'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 12, 'completion_tokens': 3}}
        with patch('repo_agent.core.provider.urllib.request.urlopen',
                   return_value=io.BytesIO(json.dumps(response).encode())) as send:
            reply = provider.respond([{'role': 'user', 'content': '你好'}], response_limits={'max_tokens': 32})
        self.assertIsInstance(reply, TextReply)
        payload = json.loads(send.call_args.args[0].data)
        self.assertNotIn('tools', payload)
        self.assertEqual(32, payload['max_tokens'])
        self.assertEqual(payload, next(event['request'] for event in events if event['type'] == 'model_input'))
        self.assertEqual(['started', 'response_received'],
                         [event['outcome'] for event in events if event['type'] == 'model_request_attempt'])
        self.assertEqual(12, next(event['usage']['prompt_tokens'] for event in events if event['type'] == 'model_usage'))

    def test_context_limit_prevents_network_request(self):
        with patch('repo_agent.core.provider.urllib.request.urlopen') as send:
            with self.assertRaises(ContextLimitError):
                self.provider().respond([{'role': 'user', 'content': 'x' * 100}], response_limits={'max_input_bytes': 10})
        send.assert_not_called()

    def test_invalid_text_responses_are_rejected(self):
        for content in ('', ' ', '\x00', None):
            data = {'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]}
            with self.subTest(content=content), patch('repo_agent.core.provider.urllib.request.urlopen',
                                                     return_value=io.BytesIO(json.dumps(data).encode())):
                with self.assertRaises(ProviderError):
                    self.provider().respond([])

    def test_contract_rejects_unsupported_permissions_and_preserves_text(self):
        text = '中文😀\n第二行'
        self.assertEqual(text, validate_turn(submission(text))['content'])
        for change in ({'expectedRevision': True}, {'mode': 'execute'}, {'content': '\x00'}, {'content': 'x' * 8001}):
            with self.subTest(change=change), self.assertRaises(TaskError):
                validate_turn({**submission(), **change})


@unittest.skipUnless(os.environ.get('CODING_AGENT_TEST_DATABASE_URL'), 'Dedicated test database not configured')
class ChatDatabaseAcceptance(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        database = os.environ['CODING_AGENT_TEST_DATABASE_URL']
        schema = 'chat_acceptance_' + uuid4().hex

        def schema_command(statement):
            with psycopg.connect(database, connect_timeout=5) as conn:
                conn.execute(sql.SQL(statement).format(sql.Identifier(schema)))

        schema_command('CREATE SCHEMA {}')
        self.addCleanup(schema_command, 'DROP SCHEMA {} CASCADE')
        options = conninfo_to_dict(database).get('options', '')
        self.database_url = make_conninfo(database, options=f'{options} -csearch_path={schema}'.strip())
        output = os.environ.get('CHAT_ACCEPTANCE_CASES_ROOT')
        if output:
            self.root = Path(output) / hashlib.sha256(self._testMethodName.encode()).hexdigest()[:8]
            self.root.mkdir(parents=True, exist_ok=False)
            (self.root / 'case.json').write_text(json.dumps({'test': self._testMethodName}), encoding='utf-8')
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        self.service = build_fixture(self.root, database_url=self.database_url)
        self.addCleanup(self.service.close)
        self.chat = self.service.chat
        self.repository = self.chat.store
        self.execution = self.service.chat_execution
        FixtureChatModel.calls.clear()
        self.conversation = self.chat.create({'title': '验收会话'})
        self.cid = self.conversation['conversationId']

    def submit(self, content='第一轮问题', request_id='request-1'):
        revision = self.chat.get(self.cid)['revision']
        return self.chat.submit(self.cid, submission(content, revision, request_id))[1]

    def execute(self):
        claim = self.repository.claim('acceptance-worker')
        self.assertIsNotNone(claim)
        self.execution.run_claimed(claim)
        return claim

    def recover(self):
        stop = threading.Event()
        stop.set()
        WorkerPool(self.service.execution.queue, self.service.execution.execution,
                   chat=self.execution, poll_seconds=0.05).run(stop)

    def start_worker(self, label):
        log = (self.root / (label + '.log')).open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-m', 'tests.chat_fixture', 'worker', '--root', str(self.root)],
                                   stdout=log, stderr=subprocess.STDOUT,
                                   env={**os.environ, 'CODING_AGENT_TEST_DATABASE_URL': self.database_url},
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        def stop_worker():
            (self.root / 'stop').touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()

        self.addCleanup(stop_worker)
        return process

    def wait_for(self, condition):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail('Worker acceptance condition timed out')

    def test_two_turn_conversation_and_material_pagination(self):
        first = self.submit('记住数字 17')
        self.execute()
        self.assertEqual('completed', self.chat.turn(self.cid, first['turnId'])['status'])
        self.submit('请引用上一轮', 'request-2')
        self.execute()
        self.assertIn('记住数字 17', [message['content'] for message in FixtureChatModel.calls[-1]])
        self.assertTrue(any(message['role'] == 'assistant' for message in FixtureChatModel.calls[-1]))
        messages = self.chat.messages(self.cid)['messages']
        self.assertEqual(['user', 'assistant', 'user', 'assistant'], [message['role'] for message in messages])
        offset, parts = 0, []
        while True:
            page = self.chat.artifact(self.cid, first['turnId'], 'context', offset=offset, limit=13)
            self.assertTrue(page['integrityChecked'])
            parts.append(page['content'])
            offset = page['nextOffset']
            if not page['hasMore']:
                break
        self.assertEqual(self.cid, json.loads(''.join(parts))['conversationId'])
        self.assertIsNone(self.chat.get(self.cid)['activeTurnId'])

    def test_queued_turn_uses_completed_prior_reply(self):
        first = self.submit('记住数字 17')
        second = self.submit('请引用上一轮', 'request-2')
        self.assertEqual(first['turnId'], self.chat.get(self.cid)['activeTurnId'])
        self.assertEqual(second['turnId'], self.chat.get(self.cid)['latestTurnId'])
        self.execute()
        self.assertEqual(second['turnId'], self.chat.get(self.cid)['activeTurnId'])
        self.execute()
        actual = FixtureChatModel.calls[-1]
        self.assertEqual(['system', 'user', 'assistant', 'user', 'user'],
                         [message['role'] for message in actual])
        self.assertEqual('记住数字 17', actual[1]['content'])
        self.assertIn('脚本回复：记住数字 17', actual[2]['content'])
        self.assertEqual([], json.loads(actual[3]['content'])['program_current_turn']['allowedTools'])
        self.assertEqual('请引用上一轮', actual[4]['content'])
        self.assertEqual('completed', self.chat.turn(self.cid, second['turnId'])['status'])

    def test_project_turn_uses_existing_read_tool_without_writes(self):
        project = self.root / 'read-project'
        project.mkdir()
        (project / 'entry.py').write_text('def entry():\n    return 17\n', encoding='utf-8')
        project_id = self.service.projects.register_local(project.resolve(), 'directory')
        self.cid = self.chat.create({'title': '项目阅读', 'workspaceRef': project_id})['conversationId']

        class ReadModel:
            def __init__(self):
                self.step = 0

            def respond(self, messages, *, tools, response_limits):
                self.step += 1
                if self.step == 1:
                    assert {item['function']['name'] for item in tools} == {'list_files', 'search_code', 'read_file'}
                    return ToolCallReply(Decision('read_file', {'path': 'entry.py', 'start': 1, 'end': 2}))
                assert messages[-1]['role'] == 'tool'
                assert 'return 17' in messages[-1]['content']
                return TextReply('entry.py:1-2 返回 17', {'role': 'assistant', 'content': 'entry.py:1-2 返回 17'})

        self.execution.model_factory = ReadModel
        before = (project / 'entry.py').read_bytes()
        turn = self.submit('入口函数在哪里？')
        self.execute()
        self.assertEqual('completed', self.chat.turn(self.cid, turn['turnId'])['status'])
        self.assertEqual(2, self.chat.turn(self.cid, turn['turnId'])['metrics']['modelDecisions'])
        self.assertEqual(1, self.chat.turn(self.cid, turn['turnId'])['metrics']['toolActions'])
        self.assertEqual(before, (project / 'entry.py').read_bytes())
        seen = []

        class FollowModel:
            def respond(self, messages, *, tools, response_limits):
                seen.extend(messages)
                return TextReply('上一轮确实读取了 entry.py。',
                                 {'role': 'assistant', 'content': '上一轮确实读取了 entry.py。'})

        self.execution.model_factory = FollowModel
        follow = self.submit('你上一轮真的读过 entry.py 吗？', 'request-2')
        self.execute()
        self.assertEqual('completed', self.chat.turn(self.cid, follow['turnId'])['status'])
        records = [json.loads(message['content'])['program_observations'] for message in seen
                   if message['role'] == 'user' and message['content'].startswith('{"program_observations":')]
        self.assertEqual(1, len(records))
        self.assertEqual(f'chat_obs_{turn["turnId"]}_1', records[0][0]['observationId'])
        self.assertEqual('read_file', records[0][0]['action'])
        self.assertIn('return 17', records[0][0]['result']['data']['content'])
        self.assertEqual(hashlib.sha256(before).hexdigest(), records[0][0]['sha256'])
        current_policy = json.loads(seen[-2]['content'])['program_current_turn']
        self.assertCountEqual(['list_files', 'search_code', 'read_file'], current_policy['allowedTools'])
        self.assertFalse(current_policy['allowWrites'])

    def test_chat_execute_reuses_coding_task_and_returns_actual_result(self):
        project = self.root / 'edit-project'
        project.mkdir()
        target = project / 'entry.py'
        target.write_text('def entry():\n    return 17\n', encoding='utf-8')
        project_id = self.service.projects.register_local(project.resolve(), 'directory')
        self.cid = self.chat.create({'title': '项目修改', 'workspaceRef': project_id})['conversationId']

        class RepairModel:
            def __init__(self):
                self.step = 0

            def decide(self, messages):
                self.step += 1
                if self.step == 1:
                    return Decision('read_file', {'path': 'entry.py', 'start': 1, 'end': 2})
                if self.step == 2:
                    observed = latest_tool_result(messages)
                    return Decision('replace_lines', {'snapshot_id': observed['data']['snapshot_id'],
                                                      'start': 2, 'end': 2,
                                                      'new_text': '    return 18\n'})
                return Decision('finish', {'answer': '已把返回值改为 18', 'outcome': 'completed'})

        self.service.execution.execution.model_factory = RepairModel
        request = submission('按刚才的要求把返回值改为 18')
        request['mode'] = 'execute'
        request['execution'] = {'kind': 'fix_bug', 'allowedPaths': ['entry.py'],
                                'workspaceMode': 'direct', 'allowTests': False,
                                'checkTargets': [], 'maxSteps': 6}
        code, submitted = self.chat.submit(self.cid, request)
        self.assertEqual(202, code)
        self.execute()  # Chat bridge only; the coding queue remains independent.
        turn = self.chat.turn(self.cid, submitted['turnId'])
        self.assertEqual(submitted['turnId'], turn['taskId'])
        task_id, execution_id = self.service.execution.queue.claim('acceptance-worker')
        self.assertEqual(turn['taskId'], task_id)
        state = self.service.execution.execution.run_claimed(task_id, execution_id)
        self.assertEqual('terminated', state['status'])
        self.service.execution.queue.finish(task_id, 'acceptance-worker', terminated=True,
                                            reason=state['stopReason'])
        self.execution.reconcile()
        finished = self.chat.turn(self.cid, submitted['turnId'])
        self.assertEqual('completed', finished['status'])
        self.assertEqual('agent_reported_finished', finished['executionEvidence']['execution_status'])
        self.assertEqual('needs_review', finished['executionEvidence']['acceptance_status'])
        self.assertEqual('skipped', finished['executionEvidence']['finalChecks']['status'])
        self.assertIn('return 18', target.read_text(encoding='utf-8'))
        follow = self.submit('为什么这样修改？', 'request-2')
        self.assertEqual('queued', follow['status'])
        seen = []

        class FollowModel:
            def respond(self, messages, *, tools, response_limits):
                seen.extend(messages)
                return TextReply('依据实际修改与检查记录回答。',
                                 {'role': 'assistant', 'content': '依据实际修改与检查记录回答。'})

        self.execution.model_factory = FollowModel
        self.execute()
        self.assertEqual('completed', self.chat.turn(self.cid, follow['turnId'])['status'])
        self.assertTrue(any('program_execution_record' in message['content'] for message in seen
                            if message['role'] == 'user'))

    def test_execute_bridge_keeps_long_history_as_separate_context(self):
        project = self.root / 'long-context-project'
        project.mkdir()
        (project / 'entry.py').write_text('value = 1\n', encoding='utf-8')
        project_id = self.service.projects.register_local(project.resolve(), 'directory')
        self.cid = self.chat.create({'workspaceRef': project_id})['conversationId']

        class ProjectDiscussionModel:
            def respond(self, messages, *, tools, response_limits):
                assert {tool['function']['name'] for tool in tools} == {'list_files', 'search_code', 'read_file'}
                reply = '已记录需求背景。'
                return TextReply(reply, {'role': 'assistant', 'content': reply})

        self.execution.model_factory = ProjectDiscussionModel
        background = '背景：' + 'x' * 4500
        first = self.submit(background)
        self.execute()
        self.assertEqual('completed', self.chat.turn(self.cid, first['turnId'])['status'])
        request = submission('把 value 改成 2', revision=self.chat.get(self.cid)['revision'],
                             request_id='request-2')
        request.update(mode='execute', execution={'kind': 'fix_bug', 'allowedPaths': ['entry.py'],
                                                  'workspaceMode': 'direct', 'allowTests': False,
                                                  'checkTargets': [], 'maxSteps': 3})
        _, submitted = self.chat.submit(self.cid, request)
        self.execute()
        contract = self.service.store.get(submitted['turnId'])['contract']
        self.assertEqual('把 value 改成 2', contract['current'])
        context = json.loads(contract['conversationContext'])
        self.assertEqual({'role': 'user', 'content': background}, context['messages'][0])
        self.assertEqual({'role': 'assistant', 'content': '已记录需求背景。'}, context['messages'][1])
        self.assertNotIn(contract['current'], [message['content'] for message in context['messages']])

    def test_idempotency_and_competing_submissions(self):
        payload = submission()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.chat.submit(self.cid, payload), range(2)))
        self.assertEqual([200, 202], sorted(status for status, _ in results))
        self.assertEqual(1, len(self.chat.messages(self.cid)['messages']))
        self.assertEqual(results[0][1]['turnId'], results[1][1]['turnId'])
        with self.assertRaises(TaskError) as error:
            self.chat.submit(self.cid, {**payload, 'content': '另一个需求'})
        self.assertEqual('REQUEST_KEY_REUSED', error.exception.code)
        with self.assertRaises(TaskError) as error:
            self.chat.submit(self.cid, submission(request_id='other'))
        self.assertEqual('REVISION_CONFLICT', error.exception.code)

    def test_restart_and_namespace_isolation(self):
        turn = self.submit()
        second = build_fixture(self.root, database_url=self.database_url)
        self.addCleanup(second.close)
        self.assertEqual(turn['turnId'], second.chat.get(self.cid)['activeTurnId'])
        other = build_fixture(self.root / 'other-data', database_url=self.database_url)
        self.addCleanup(other.close)
        with self.assertRaises(TaskError) as error:
            other.chat.get(self.cid)
        self.assertEqual('CONVERSATION_NOT_FOUND', error.exception.code)

    def test_heartbeat_and_stale_claim_fencing(self):
        turn = self.submit()
        claim = self.repository.claim('acceptance-worker')
        with self.service.store._connection() as conn:
            conn.execute("UPDATE chat_turn_jobs SET heartbeat_at=now()-interval '30 seconds' WHERE turn_id=%s", (turn['turnId'],))
        self.assertEqual('unknown', self.chat.turn(self.cid, turn['turnId'])['status'])
        with self.assertRaises(TaskError) as error:
            self.repository.record_event({**claim, 'claimId': 'f' * 32}, {'type': 'stale'})
        self.assertEqual('CHAT_CLAIM_LOST', error.exception.code)
        self.execution.run_claimed(claim)
        self.assertEqual('completed', self.chat.turn(self.cid, turn['turnId'])['status'])

    def test_provider_failure_and_context_limit_release_conversation(self):
        failed = self.submit('[FAIL]')
        self.execute()
        self.assertEqual('PROVIDER_ERROR', self.chat.turn(self.cid, failed['turnId'])['stopReason'])
        limited = self.submit('x' * 300, 'request-2')
        self.execution.configure(max_input_bytes=100, max_output_tokens=32)
        count = len(FixtureChatModel.calls)
        self.execute()
        self.assertEqual(count, len(FixtureChatModel.calls))
        self.assertEqual('CONTEXT_LIMIT', self.chat.turn(self.cid, limited['turnId'])['stopReason'])
        self.assertIsNone(self.chat.get(self.cid)['activeTurnId'])

    def test_saved_result_recovers_without_repeating_model(self):
        turn = self.submit()
        with patch.object(self.repository, 'finish', side_effect=TaskError(503, 'DATABASE_UNAVAILABLE', 'Injected commit failure')):
            with self.assertRaises(TaskError):
                self.execute()
        count = len(FixtureChatModel.calls)
        self.recover()
        restored = self.chat.turn(self.cid, turn['turnId'])
        self.assertEqual('completed', restored['status'])
        self.assertTrue(restored['recovered'])
        self.assertEqual(count, len(FixtureChatModel.calls))
        self.assertEqual(2, len(self.chat.messages(self.cid)['messages']))

    def test_interrupted_claim_becomes_failure_without_model_replay(self):
        turn = self.submit()
        self.repository.claim('interrupted-worker')
        self.recover()
        self.assertEqual('WORKER_INTERRUPTED', self.chat.turn(self.cid, turn['turnId'])['stopReason'])
        self.assertEqual([], FixtureChatModel.calls)
        self.assertIsNone(self.chat.get(self.cid)['activeTurnId'])

    def test_changed_material_rejected_and_recovery_keeps_failure(self):
        turn = self.submit()
        with patch.object(self.repository, 'finish', side_effect=TaskError(503, 'DATABASE_UNAVAILABLE', 'Injected commit failure')):
            with self.assertRaises(TaskError):
                claim = self.repository.claim('acceptance-worker')
                self.execution.run_claimed(claim)
        directory = self.execution._directory(claim)
        original = (directory / 'reply.json').read_bytes()
        (directory / 'reply-before-mutation.json').write_bytes(original)
        (directory / 'reply.json').write_bytes(original + b' ')
        self.recover()
        self.assertEqual('RECOVERY_EVIDENCE_INVALID', self.chat.turn(self.cid, turn['turnId'])['stopReason'])
        self.assertEqual(1, len(FixtureChatModel.calls))

    def test_artifact_hash_changes_and_cross_conversation_reads_are_rejected(self):
        turn = self.submit('材料 😀')
        self.execute()
        other = self.chat.create({})
        with self.assertRaises(TaskError) as error:
            self.chat.artifact(other['conversationId'], turn['turnId'], 'context')
        self.assertEqual('TURN_NOT_FOUND', error.exception.code)
        reference = self.chat.turn(self.cid, turn['turnId'])['artifacts']['context']
        target = self.service.store.runs_root / reference['path']
        original = target.read_bytes()
        target.with_name('context-before-mutation.json').write_bytes(original)
        target.write_bytes(original + b' ')
        with self.assertRaises(TaskError) as error:
            self.chat.artifact(self.cid, turn['turnId'], 'context')
        self.assertEqual('ARTIFACT_CHANGED', error.exception.code)

    def test_actual_worker_termination_and_restart_preserve_failure_then_followup(self):
        turn = self.submit('[HOLD]')
        process = self.start_worker('before-kill')
        self.wait_for(lambda: (self.root / 'model-entered').exists())
        process.kill()
        process.wait(timeout=5)
        self.start_worker('after-restart')
        self.wait_for(lambda: self.chat.turn(self.cid, turn['turnId'])['status'] == 'failed')
        self.assertEqual('WORKER_INTERRUPTED', self.chat.turn(self.cid, turn['turnId'])['stopReason'])
        followup = self.submit('重启后的追问', 'followup')
        self.wait_for(lambda: self.chat.turn(self.cid, followup['turnId'])['status'] == 'completed')
        self.assertEqual(['user', 'user', 'assistant'], [message['role'] for message in self.chat.messages(self.cid)['messages']])

    def test_one_worker_pool_consumes_chat_and_ordinary_coding_queues(self):
        target = self.root / 'target'
        target.mkdir()
        (target / 'sample.py').write_text('value = 1\n', encoding='utf-8')
        task = self.service.create({'instruction': '只读分析', 'targetPath': str(target),
                                    'kind': 'read_code', 'workspaceMode': 'direct', 'maxSteps': 3})
        self.service.execution.start(task['taskId'], {})
        turn = self.submit('[SLOW] 混合队列')
        self.start_worker('mixed-worker')
        self.wait_for(lambda: self.service.execution.get(task['taskId'])['status'] == 'terminated'
                      and self.chat.turn(self.cid, turn['turnId'])['status'] == 'completed')
        self.assertEqual('terminated', self.service.execution.get(task['taskId'])['status'])
        self.assertEqual('value = 1\n', (target / 'sample.py').read_text(encoding='utf-8'))

    def test_http_routes_messages_artifacts_and_worker_process(self):
        server = create_server(self.service, port=0, frontend=Path(__file__).resolve().parents[1] / 'frontend')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as response:
                return response.status, response.read()

        payload = json.dumps(submission('HTTP 中文😀')).encode('utf-8')
        request = urllib.request.Request(base + f'/api/conversations/{self.cid}/turns', data=payload,
                                         headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(202, response.status)
            turn = json.load(response)
        process = self.start_worker('worker')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self.chat.turn(self.cid, turn['turnId'])['status']
            if status in ('completed', 'failed') or process.poll() is not None:
                break
            time.sleep(0.05)
        self.assertEqual('completed', status)
        self.assertEqual(200, get('/chat.js')[0])
        status, raw = get(f'/api/conversations/{self.cid}/messages')
        self.assertEqual(2, len(json.loads(raw)['messages']))
        status, raw = get(f'/api/conversations/{self.cid}/turns/{turn["turnId"]}/artifacts/context?offset=0&limit=12')
        self.assertTrue(json.loads(raw)['hasMore'])
