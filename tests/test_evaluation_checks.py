"""Developer-check feedback and evidence boundaries; no real Docker/model calls."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from repo_agent.core.agent import Agent
from repo_agent.core.context import latest_tool_result
from repo_agent.core.decisions import Decision
from repo_agent.core.progress import RunProgress
from repo_agent.evaluation.artifacts import load_run
from repo_agent.evaluation.check_runner import archive_files, check_result, execute_setup, test_command as build_check_command
from repo_agent.evaluation.development_checks import DevelopmentChecks, EvaluationTools, relative_file
from repo_agent.evaluation.swebench import export_patch, prepare


class EvaluationCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        def git(*args):
            return subprocess.run(['git', '-c', 'core.hooksPath=' + str(self.root / 'no-hooks'),
                                   '-c', 'commit.gpgsign=false', '-c', 'core.autocrlf=false',
                                   '-C', str(self.source), *args], capture_output=True, check=True, timeout=30).stdout
        git('init')
        git('config', 'user.name', 'Offline Check Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (self.source / 'app.py').write_bytes(b'value = 1\n')
        (self.source / 'test_existing.py').write_bytes(b'def test_value(): assert True\n')
        (self.source / 'validation').mkdir()
        (self.source / 'validation/scenarios.py').write_bytes(
            b'class TestBehavior:\n    def test_value(self):\n        assert 1 == 1\n')
        (self.source / 'setup.py').write_bytes(b'setting = 1\n')
        git('add', '.')
        git('commit', '-m', 'fixture')
        commit = git('rev-parse', 'HEAD').decode().strip()
        row = {'instance_id': 'acme__demo-1', 'repo': 'acme/demo', 'base_commit': commit,
               'problem_statement': 'Change value to 2', 'version': '1', 'patch': 'HIDDEN_GOLD',
               'test_patch': 'HIDDEN_R', 'FAIL_TO_PASS': '["hidden_target"]', 'PASS_TO_PASS': '[]'}
        record = self.root / 'input.json'
        record.write_text(json.dumps(row), encoding='utf-8')
        self.directory = prepare(record, row['instance_id'], self.source,
                                 runs_root=self.root / 'runs', workspaces_root=self.root / 'workspaces')
        _, run, self.task = load_run(self.directory)
        self.workspace = Path(run['workspace'])
        self.checks = DevelopmentChecks(self.directory, self.workspace, self.task, image='sha256:' + 'a' * 64)
        self.tools = EvaluationTools(self.workspace, checks=self.checks,
            contract={'kind': 'fix_bug', 'allowTests': True, 'allowedPaths': ['.'],
                      'checkTargets': [], 'workspaceMode': 'direct', 'sourceKind': 'git'})

    def test_check_material_is_external_editable_and_not_a_business_patch(self):
        created = self.tools.execute('create_file', {'path': '.agent-checks/check_value.py', 'content': 'assert True\n'})
        self.assertTrue(created['ok'])
        self.assertEqual('agent_check', created['data']['artifact_role'])
        self.assertFalse((self.workspace / '.agent-checks').exists())
        read = self.tools.execute('read_file', {'path': '.agent-checks/check_value.py', 'start': 1, 'end': 1})
        with patch('subprocess.run', side_effect=AssertionError('Scratch edits must not invoke outer Git')):
            changed = self.tools.execute('replace_lines', {'snapshot_id': read['data']['snapshot_id'],
                'start': 1, 'end': 1, 'new_text': 'assert 1 == 1\n'})
        self.assertTrue(changed['ok'])
        self.assertEqual(b'assert 1 == 1\n', (self.checks.root / 'check_value.py').read_bytes())
        ref = export_patch(self.directory, self.workspace, self.task['baseCommit'])
        self.assertEqual(0, ref['bytes'])
        blocked = self.tools.execute('create_file', {'path': 'test_new.py', 'content': 'assert True'})
        self.assertFalse(blocked['ok'])

    def test_traversal_and_nonexistent_or_non_test_source_targets_are_rejected(self):
        for target in ('../escape.py', '/etc/check.py', 'C:/check.py', 'a/../b.py', 'a\\b.py'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                relative_file(target)
        for target in ('.', 'app.py', 'setup.py', 'test_missing.py', '.agent-checks/missing.py',
                       'validation/scenarios.py::TestBehavior::not_a_test', '-q', 'python -m pytest'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.checks.run(target)

    def test_nonstandard_frozen_tests_have_separate_admission_and_write_protection(self):
        path = 'validation/scenarios.py'
        target = path + '::TestBehavior::test_value'
        self.assertEqual((self.workspace / path).read_bytes(), self.checks.targets.read(target))
        self.assertEqual((self.workspace / path).read_bytes(), self.checks.targets.read(path + '::TestBehavior'))
        self.assertTrue(self.tools.editable('app.py'))
        self.assertFalse(self.tools.editable(path))
        read = self.tools.execute('read_file', {'path': path})
        result = self.tools.execute('replace_lines', {'snapshot_id': read['data']['snapshot_id'],
                                     'start': 3, 'end': 3, 'new_text': '        assert False\n'})
        self.assertFalse(result['ok'])
        original = self.workspace / path
        relocated = original.rename(original.with_name('relocated.py'))
        try:
            with self.assertRaisesRegex(ValueError, 'Rename and copy patches require review'):
                self.checks.targets.export(self.directory)
        finally:
            relocated.rename(original)
        original.write_bytes(b'def test_changed(): assert True\n')
        with self.assertRaisesRegex(ValueError, 'differs from the frozen'):
            self.checks.targets.read(path)
        with self.assertRaisesRegex(ValueError, 'Frozen repository tests changed'):
            self.checks.targets.export(self.directory)
        self.assertFalse((self.directory / 'candidate.patch').exists())

    def test_temporary_edits_do_not_satisfy_implementation_or_keep_old_check_evidence(self):
        progress = RunProgress()
        edit = {'ok': True, 'data': {'applied': True, 'artifact_role': 'agent_check'}}
        progress.observe('create_file', edit)
        self.assertEqual(0, progress.edit_revision)
        passed = progress.observe('run_tests', {'ok': True, 'data': {'passed': True}})
        progress.update([{'id': 'check', 'goal': 'Check behavior', 'status': 'completed',
                          'evidence_refs': [passed], 'blocked_reason': ''}], 'fixture')
        progress.observe('replace_lines', edit)
        self.assertIsNone(progress.last_test_passed)
        self.assertIn(passed, progress.snapshot()['stale_evidence_refs'])

    def test_failed_check_feedback_can_drive_edit_and_recheck_in_same_agent_loop(self):
        decisions = [Decision('create_file', {'path': '.agent-checks/check_value.py', 'content': 'assert True\n'}),
                     Decision('run_tests', {'target': '.agent-checks/check_value.py'}),
                     Decision('read_file', {'path': 'app.py', 'start': 1, 'end': 1})]
        fixture = self
        class Model:
            calls = 0
            def decide(self, messages):
                self.calls += 1
                if self.calls == 3:
                    feedback = latest_tool_result(messages)
                    fixture.assertFalse(feedback['data']['passed'])
                    fixture.assertIn('AssertionError', feedback['data']['stdout'])
                if self.calls <= 3:
                    return decisions[self.calls - 1]
                if self.calls == 4:
                    observation = latest_tool_result(messages)
                    return Decision('replace_lines', {'snapshot_id': observation['data']['snapshot_id'],
                        'start': 1, 'end': 1, 'new_text': 'value = 2\n'})
                if self.calls == 5:
                    return Decision('run_tests', {'target': '.agent-checks/check_value.py'})
                return Decision('finish', {'answer': 'Changed value; developer check passed, official grade unknown',
                    'outcome': 'completed', 'evidence': 'Business edit and developer feedback'})
        def check(target):
            passed = (self.workspace / 'app.py').read_bytes() == b'value = 2\n'
            return {'passed': passed, 'stdout': 'OK' if passed else 'AssertionError', 'officialGrade': False}
        with patch.object(self.checks, 'run', side_effect=check):
            result = Agent(Model(), self.tools, trace_dir=self.directory / 'trace', max_steps=6).run('Change value to 2')
        self.assertEqual('agent_reported_finished', result['status'])
        self.assertTrue(result['last_test_passed'])
        self.assertEqual(1, result['progress']['edit_revision'])
        self.assertFalse((self.directory / 'grades').exists())

    def test_check_only_cannot_finish_an_implementation_task(self):
        class Model:
            calls = 0
            def decide(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return Decision('create_file', {'path': '.agent-checks/check.py', 'content': 'pass\n'})
                return Decision('finish', {'answer': 'Done', 'outcome': 'completed'})
        result = Agent(Model(), self.tools, trace_dir=self.directory / 'trace', max_steps=2).run('Fix the bug')
        self.assertEqual('step_limit', result['status'])
        self.assertEqual(0, result['progress']['edit_revision'])

    def test_failed_check_blocks_completed_even_without_an_explicit_plan(self):
        progress = RunProgress()
        progress.observe('run_tests', {'ok': True, 'data': {'passed': False}})
        self.assertTrue(progress.completion_issues('completed'))
        self.assertEqual([], progress.completion_issues('incomplete'))

    def test_failed_check_can_finish_incomplete_when_only_finish_is_offered(self):
        from repo_agent.evaluation.runner import _BudgetedModel
        from repo_agent.core.decisions import TOOL_DEFINITIONS

        class Counter:
            output_tokens = 10
            input_tokens = 50
            counter_name = 'fixture'
            def require(self, messages, tools):
                return 10

        class Model:
            def set_event_sink(self, sink):
                self.sink = sink
            def get_tool_definitions(self):
                return TOOL_DEFINITIONS
            def get_run_metadata(self):
                return {'transport_retry': {'max_attempts': 1}}
            def decide_with_limits(self, messages, limits):
                self.sink({'type': 'model_usage', 'available': True,
                           'usage': {'prompt_tokens': 5, 'completion_tokens': 3}})
                return Decision('run_tests', {'target': 'test_existing.py'})
            def decide_with_tools(self, messages, limits, tool_names):
                self.offered = tool_names
                self.report_state = json.loads(messages[-1]['content'])
                return Decision('finish', {'answer': 'Check failed; work remains incomplete.',
                                            'outcome': 'incomplete', 'evidence': 'Developer check failed'})

        model = Model()
        guarded = _BudgetedModel(model, Counter(), 175, 30)
        with patch.object(self.checks, 'run', return_value={'passed': False, 'officialGrade': False}):
            result = Agent(guarded, self.tools, trace_dir=self.directory / 'trace',
                           max_steps=3, token_budget=Counter()).run('Change value')
        self.assertEqual(['finish'], model.offered)
        self.assertEqual('incomplete', result['outcome'])
        self.assertFalse(result['last_test_passed'])
        self.assertEqual('test_existing.py', model.report_state['latestCheck']['target'])
        self.assertFalse(model.report_state['latestCheck']['passed'])
        self.assertNotIn('completed', model.report_state['allowedOutcomes'])

    def test_container_snapshot_excludes_hidden_inputs_and_persists_old_check_revision(self):
        self.tools.execute('create_file', {'path': '.agent-checks/check.py', 'content': 'assert True\n'})
        original_run = subprocess.run
        def external(command, **kwargs):
            if command[0] == 'git':
                return original_run(command, **kwargs)
            directory = Path(command[-1]).parent
            request = json.loads((directory / 'request.json').read_bytes())
            self.assertFalse(request['officialTestsIncluded'])
            self.assertNotIn('HIDDEN_', (directory / 'request.json').read_text())
            (directory / 'result.json').write_text(json.dumps({'passed': False, 'exit_code': 1, 'check_status': 'failed'}))
            (directory / 'test-output.txt').write_text('AssertionError: value was 1')
            return subprocess.CompletedProcess(command, 0, b'', b'')
        with patch('repo_agent.evaluation.development_checks.subprocess.run', side_effect=external):
            result = self.tools.execute('run_tests', {'target': '.agent-checks/check.py'})
        self.assertTrue(result['ok'])
        self.assertFalse(result['data']['passed'])
        self.assertIn('AssertionError', result['data']['stdout'])
        self.assertFalse(result['data']['officialGrade'])
        snapshot = Path(result['data']['test_log']['directory']) / 'checks/check.py'
        (self.checks.root / 'check.py').write_bytes(b'assert False\n')
        self.assertEqual(b'assert True\n', snapshot.read_bytes())

    def test_native_runner_no_tests_and_timeouts_cannot_report_pass(self):
        config = {'temporary': False, 'repo': 'sympy/sympy', 'target': 'sympy/tests/test_sample.py'}
        self.assertIn('bin/test', build_check_command(config))
        self.assertFalse(check_result(config, 0, 'no tests collected')['passed'])
        self.assertFalse(check_result(config, 1, 'tests finished: 120 passed, 2 failed, 2 exceptions,')['passed'])
        self.assertTrue(check_result(config, 0, 'tests finished: 2 passed,')['passed'])
        self.assertTrue(check_result({**config, 'temporary': True}, 124, '')['timed_out'])
        with self.assertRaises(ValueError):
            archive_files([('../escape.py', b'pass')])

    def test_review_and_versions_are_frozen_with_check_snapshot(self):
        self.tools.execute('create_file', {'path': '.agent-checks/check.py', 'content': 'assert value == 2\n'})
        review = {'purpose': 'regression', 'criteria': [{
            'requirement': 'Change value to 2', 'scenario': 'read value', 'expected': '2',
            'expected_basis': 'task',
            'start_line': 1, 'end_line': 1}], 'assumptions': ''}
        original_run = subprocess.run

        def external(command, **kwargs):
            if command[0] == 'git':
                return original_run(command, **kwargs)
            directory = Path(command[-1]).parent
            request = json.loads((directory / 'request.json').read_bytes())
            self.assertTrue(request['compareBaseline'])
            self.assertTrue(request['reviewEvidence']['references_valid'])
            self.assertFalse(request['reviewEvidence']['semanticCoverageVerified'])
            (directory / 'result.json').write_text(json.dumps({
                'passed': True, 'check_status': 'passed', 'check_kind': 'model_authored_python',
                'comparison': {'status': 'assertion_failed_on_base_then_passed'}}))
            (directory / 'test-output.txt').write_text('')
            return subprocess.CompletedProcess(command, 0, b'', b'')

        with patch('repo_agent.evaluation.development_checks.subprocess.run', side_effect=external):
            result = self.tools.execute('run_tests', {'target': '.agent-checks/check.py', 'review': review})
        self.assertTrue(result['ok'])
        self.assertEqual('needs_review', result['data']['verification']['level'])
        patch_sha = result['data']['patchSha256']
        (self.checks.root / 'check.py').write_bytes(b'assert value == 3\n')
        self.assertEqual('verification_incomplete', self.checks.verification_summary(patch_sha)['level'])

    def test_unchanged_script_reuses_review_but_changed_script_expires_it(self):
        self.tools.execute('create_file', {'path': '.agent-checks/check.py', 'content': 'assert value == 2\n'})
        review = {'purpose': 'regression', 'criteria': [{
            'requirement': 'Change value to 2', 'scenario': 'read value', 'expected': '2',
            'expected_basis': 'task', 'start_line': 1, 'end_line': 1}], 'assumptions': ''}
        original_run = subprocess.run

        def external(command, **kwargs):
            if command[0] == 'git':
                return original_run(command, **kwargs)
            directory = Path(command[-1]).parent
            (directory / 'result.json').write_text(json.dumps({
                'passed': True, 'check_status': 'passed', 'check_kind': 'model_authored_python',
                'comparison': {'status': 'assertion_failed_on_base_then_passed'}}))
            (directory / 'test-output.txt').write_text('')
            return subprocess.CompletedProcess(command, 0, b'', b'')

        with patch('repo_agent.evaluation.development_checks.subprocess.run', side_effect=external):
            first = self.tools.execute('run_tests', {'target': '.agent-checks/check.py', 'review': review})
            second = self.tools.execute('run_tests', {'target': '.agent-checks/check.py'})
            self.assertFalse(first['data']['reviewEvidence']['reused'])
            self.assertTrue(second['data']['reviewEvidence']['reused'])
            (self.checks.root / 'check.py').write_text('assert value == 3\n', encoding='utf-8')
            changed = self.tools.execute('run_tests', {'target': '.agent-checks/check.py'})
        self.assertEqual('not_provided', changed['data']['reviewEvidence']['source'])
        self.assertFalse(changed['data']['reviewEvidence']['references_valid'])

    def test_setup_warning_does_not_corrupt_runtime_json_and_is_preserved(self):
        container = Mock()
        container.exec_run.return_value = SimpleNamespace(
            exit_code=0, output=(b'{"source":"/testbed/sympy/__init__.py"}\n', b'SyntaxWarning: fixture warning\n'))
        output = execute_setup(container, ['python', '-c', 'fixture'], self.root)
        self.assertEqual('/testbed/sympy/__init__.py', json.loads(output)['source'])
        record = json.loads((self.root / 'setup-output.jsonl').read_text())
        self.assertIn('SyntaxWarning', record['stderr'])
        self.assertTrue(container.exec_run.call_args.kwargs['demux'])

    def test_enabled_completion_rejection_returns_to_the_same_loop_for_a_check(self):
        fixture = self
        class Model:
            calls = 0
            def decide(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return Decision('read_file', {'path': 'app.py', 'start': 1, 'end': 1})
                if self.calls == 2:
                    return Decision('replace_lines', {'snapshot_id': latest_tool_result(messages)['data']['snapshot_id'],
                        'start': 1, 'end': 1, 'new_text': 'value = 2\n'})
                if self.calls == 4:
                    feedback = latest_tool_result(messages)
                    fixture.assertFalse(feedback['ok'])
                    fixture.assertIn('No passing developer check', feedback['error'])
                    return Decision('run_tests', {'target': 'test_existing.py'})
                return Decision('finish', {'answer': 'Changed value', 'outcome': 'completed',
                    'evidence': 'Business edit; developer checks when available'})
        with patch.object(self.checks, 'run', return_value={'passed': True, 'officialGrade': False}):
            result = Agent(Model(), self.tools, trace_dir=self.directory / 'trace', max_steps=5).run('Change value')
        self.assertEqual('completed', result['outcome'])
        self.assertEqual(5, result['steps'])
        rows = [json.loads(line) for line in Path(result['trace']).read_text(encoding='utf-8').splitlines()]
        rejections = [row for row in rows if row['type'] == 'finish_rejected']
        self.assertEqual(['CHECK_REQUIRED'], [row['reason'] for row in rejections])
        policy = next(row for row in rows if row['type'] == 'context' and row['step'] == 3)['runtime_state']['verification_policy']
        self.assertTrue(policy['required_for_completed'])
        self.assertIn('Business code changed', policy['reminder'])

    def test_check_freshness_gate_covers_both_business_and_check_edits(self):
        progress = RunProgress()
        progress.observe('replace_lines', {'ok': True, 'data': {'applied': True}})
        self.assertTrue(progress.completion_issues('completed', require_check=True))
        for role in ('agent_check', 'business'):
            progress.observe('run_tests', {'ok': True, 'data': {'passed': True}})
            self.assertEqual([], progress.completion_issues('completed', require_check=True))
            progress.observe('replace_lines', {'ok': True, 'data': {'applied': True, 'artifact_role': role}})
            self.assertTrue(progress.completion_issues('completed', require_check=True))
        self.assertEqual([], progress.completion_issues('incomplete', require_check=True))
        self.assertEqual([], progress.completion_issues('blocked', require_check=True))

    def test_reminders_escalate_after_repeated_actions_and_clear_after_a_passing_check(self):
        progress = RunProgress()
        self.assertIsNone(progress.runtime_state(1, 10, require_check=True)['verification_policy']['reminder'])
        progress.observe('replace_lines', {'ok': True, 'data': {'applied': True}})
        for _ in range(2):
            progress.observe('read_file', {'ok': True, 'data': {'file': 'app.py'}})
        policy = progress.runtime_state(4, 10, require_check=True)['verification_policy']
        self.assertIn('Prioritize', policy['reminder'])
        self.assertEqual(3, policy['actions_without_current_check'])
        progress.observe('run_tests', {'ok': True, 'data': {'passed': True}})
        policy = progress.runtime_state(5, 10, require_check=True)['verification_policy']
        self.assertEqual('passed', policy['current_check'])
        self.assertIsNone(policy['reminder'])
        progress.observe('run_tests', {'ok': False, 'error': 'infrastructure unavailable'})
        policy = progress.runtime_state(6, 10, require_check=True)['verification_policy']
        self.assertIn('failed or could not run', policy['reminder'])

    def test_disabled_policy_stays_optional_and_disabled_tools_cannot_run_checks(self):
        progress = RunProgress()
        progress.observe('replace_lines', {'ok': True, 'data': {'applied': True}})
        self.assertEqual([], progress.completion_issues('completed'))
        self.assertNotIn('verification_policy', progress.runtime_state(2, 5))
        disabled = EvaluationTools(self.workspace, checks=self.checks,
            contract={'kind': 'fix_bug', 'allowTests': False, 'allowedPaths': ['.'],
                      'checkTargets': [], 'workspaceMode': 'direct', 'sourceKind': 'git'})
        self.assertFalse(disabled.require_check_before_completion)
        with patch.object(self.checks, 'run') as checking:
            result = disabled.execute('run_tests', {'target': 'test_existing.py'})
        self.assertFalse(result['ok'])
        checking.assert_not_called()
