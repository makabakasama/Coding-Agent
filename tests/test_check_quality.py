"""Self-test quality regressions, using local fixtures and Docker substitutes."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from repo_agent.core.decisions import Decision, TOOL_DEFINITIONS
from repo_agent.core.progress import RunProgress
from repo_agent.core.check_tools import run_independent_checks
from repo_agent.core.check_execution import validate_sympy_selector
from repo_agent.evaluation.check_quality import review_evidence, verification_summary
from repo_agent.evaluation.check_runner import (
    _main, check_result, classify_result, comparison_result, run_phase, test_command as check_command,
)


class CheckQualityTests(unittest.TestCase):
    def review(self, **changes):
        return {'purpose': 'regression', 'criteria': [{
            'requirement': 'nested expression', 'scenario': 'print a nested expression',
            'expected': 'nested content is formatted', 'expected_basis': 'assumption',
            'start_line': 1, 'end_line': 1}],
            'assumptions': '', **changes}

    def record(self, **changes):
        return {'target': '.agent-checks/sample.py', 'checkId': 'fixture',
                'patchSha256': 'patch', 'inventorySha256': 'scripts', 'passed': True,
                'check_status': 'passed', 'check_kind': 'model_authored_python',
                'comparison': {'status': 'assertion_failed_on_base_then_passed'},
                'reviewEvidence': {'references_valid': True}, **changes}

    def test_optional_review_validates_quotes_and_test_lines_without_claiming_coverage(self):
        review = self.review()
        result = review_evidence(review, 'Fix the nested expression output', 'assert output == expected\n')
        self.assertTrue(result['references_valid'])
        self.assertFalse(result['semanticCoverageVerified'])
        review['criteria'][0]['end_line'] = 99
        self.assertEqual(1, result['review']['criteria'][0]['end_line'])
        invalid = review_evidence(review, 'Unrelated task', 'assert True\n')
        self.assertEqual(2, len(invalid['warnings']))
        self.assertFalse(review_evidence(None, 'task', '')['references_valid'])

    def test_repository_quotes_are_checked_against_frozen_source_and_assumptions_stay_visible(self):
        review = self.review()
        review['criteria'][0].update(expected='\\mathrm{tr}', expected_basis='repository', evidence=[{
            'path': 'sympy/printing/tests/test_latex.py', 'start_line': 1,
            'end_line': 1, 'quote': "assert result == r'\\mathrm{tr}'"}])
        reader = lambda path: {'text': "assert result == r'\\mathrm{tr}'\n", 'sha256': 'frozen-hash'}
        result = review_evidence(review, 'Fix nested expression output', 'assert output\n', reader)
        self.assertTrue(result['references_valid'])
        self.assertEqual('frozen-hash', result['review']['criteria'][0]['evidence'][0]['source_sha256'])

        review['criteria'][0]['evidence'][0]['quote'] = 'invented output convention'
        invalid = review_evidence(review, 'Fix nested expression output', 'assert output\n', reader)
        self.assertFalse(invalid['references_valid'])

    def test_expected_output_assumption_is_recorded_for_review(self):
        review = self.review()
        result = review_evidence(review, 'Fix the nested expression', 'assert output\n')
        self.assertTrue(result['references_valid'])
        self.assertEqual(1, len(result['unresolved_assumptions']))
        summary = verification_summary([self.record(reviewEvidence=result)], 'patch', 'scripts')
        self.assertIn('Some test expectations remain model assumptions', summary['reasons'])

    def test_review_extension_preserves_old_native_schema_and_calls(self):
        Decision('run_tests', {'target': 'test_existing.py'})
        Decision('run_tests', {'target': '.agent-checks/sample.py', 'review': self.review()})
        ordinary = next(t for t in TOOL_DEFINITIONS if t['function']['name'] == 'run_tests')
        self.assertNotIn('review', ordinary['function']['parameters']['properties'])

    def test_same_wrong_expectation_never_becomes_externally_verified(self):
        base = {'passed': False, 'check_status': 'failed'}
        candidate = {'passed': True, 'check_status': 'passed'}
        compared = comparison_result(base, candidate, 'regression')
        self.assertIsNone(compared['targetReproduced'])
        summary = verification_summary([self.record(comparison=compared)], 'patch', 'scripts')
        self.assertEqual('needs_review', summary['level'])
        self.assertEqual('not_run', summary['repositoryRegression'])
        self.assertFalse(summary['semanticCoverageVerified'])

    def test_summary_keeps_current_regression_failure_and_expires_versions(self):
        failed = self.record(target='test_existing.py', passed=False, check_status='failed',
                             check_kind='existing_repository_tests')
        records = [failed, self.record()]
        summary = verification_summary(records, 'patch', 'scripts')
        self.assertEqual('developer_check_failed', summary['level'])
        self.assertEqual('verification_incomplete', verification_summary(records, 'new_patch', 'scripts')['level'])
        self.assertEqual('developer_check_failed', verification_summary(records, 'patch', 'new_scripts')['level'])
        records.append({**failed, 'passed': True, 'check_status': 'passed'})
        summary = verification_summary(records, 'patch', 'scripts')
        self.assertEqual('needs_review', summary['level'])
        self.assertEqual('passed', summary['repositoryRegression'])

    def test_repository_evidence_survives_scratch_change_but_not_source_or_environment_change(self):
        record = self.record(target='tests/test_existing.py', check_kind='existing_repository_tests',
                             environmentSha256='testbed-a')
        before = verification_summary([record], 'patch', 'changed-scratch', environment_sha256='testbed-a')
        self.assertEqual('passed', before['repositoryRegression'])
        self.assertEqual('not_run', verification_summary(
            [record], 'patch', 'changed-scratch', environment_sha256='testbed-b')['repositoryRegression'])
        progress = RunProgress()
        ref = progress.observe('run_tests', {'ok': True, 'data': {
            **record, 'verification': before, 'test_log': {'id': record['checkId']}}})
        progress.update([{'id': 'verify', 'goal': 'Check', 'status': 'completed',
                          'blocked_reason': '', 'evidence_refs': [ref]}], 'completed')
        progress.observe('create_file', {'ok': True, 'data': {
            'applied': True, 'artifact_role': 'agent_check', 'verification': before}})
        self.assertEqual([], progress.completion_issues('completed', require_check=True))
        progress.observe('replace_lines', {'ok': True, 'data': {'applied': True}})
        self.assertEqual([ref], progress.snapshot()['stale_evidence_refs'])

    def test_temporary_exception_chain_keeps_outer_error_without_inventing_assertion_values(self):
        output = ('Traceback (most recent call last):\n  File "<string>", line 1\n'
                  'SyntaxError: unexpected EOF\n\nDuring handling of the above exception, another exception occurred:\n\n'
                  'Traceback (most recent call last):\n  File "/tmp/agent-checks/matadd.py", line 12\n'
                  "    assert render(expr)\nsympy.core.sympify.SympifyError: invalid label\n"
                  'SyntaxError: unexpected EOF while parsing\n')
        result = check_result({'temporary': True, 'repo': 'sympy/sympy'}, 1, output)
        self.assertEqual('test_execution', result['failure_stage'])
        diagnostic = result['diagnostic']
        self.assertEqual('sympy.core.sympify.SympifyError', diagnostic['exception'])
        self.assertEqual(['SyntaxError', 'sympy.core.sympify.SympifyError'],
                         [item['exception'] for item in diagnostic['exception_chain']])
        self.assertEqual(12, diagnostic['location']['line'])
        self.assertEqual('unavailable', diagnostic['values']['status'])

    def test_django_warning_inside_script_is_not_a_startup_failure(self):
        output = ('Traceback (most recent call last):\n'
                  '  File "/tmp/agent-checks/media.py", line 33\n    assert form.media\n'
                  'django.forms.widgets.MediaOrderConflictWarning: duplicate media\n')
        config = {'temporary': True, 'repo': 'django/django'}
        result = check_result(config, 1, output)
        self.assertEqual('test_execution', result['failure_stage'])
        self.assertIn('MediaOrderConflictWarning', result['failure_detail'])
        startup = check_result(config, 78, output + 'Developer check: Django test initialization failed\n')
        self.assertEqual('startup', startup['failure_stage'])

    def test_native_reported_diff_is_preserved_without_guessing_values(self):
        source = ('FAIL: test_media\nTraceback (most recent call last):\n'
                  '  File "tests/test_media.py", line 8\n'
                  '    self.assertEqual(actual, expected)\n'
                  'AssertionError: Items differ\n'
                  '- actual as shown by unittest\n+ expected as shown by unittest\n'
                  '------\nRan 1 test in 0.1s\nFAILED (failures=1)\n')
        config = {'temporary': False, 'repo': 'django/django'}
        values = check_result(config, 1, source)['diagnostic']['values']
        self.assertEqual('runner_diff', values['status'])
        self.assertIn('- actual as shown by unittest', values['diff'])
        temporary = check_result({'temporary': True, 'repo': 'django/django'}, 1, source)
        self.assertEqual('unavailable', temporary['diagnostic']['values']['status'])

    def test_named_sympy_test_is_independent_of_a_failed_temporary_check(self):
        target = 'sympy/printing/tests/test_pretty.py::test_MatrixSymbol_printing'
        config = {'temporary': False, 'repo': 'sympy/sympy', 'target': target}
        validate_sympy_selector(b'def test_MatrixSymbol_printing(): pass\n', target)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            validate_sympy_selector(b'def test_MatrixSymbol_printing(): pass\n'
                                    b'def test_MatrixSymbol_printing_more(): pass\n', target)
        self.assertEqual(['sympy/printing/tests/test_pretty.py', '-k', 'test_MatrixSymbol_printing'],
                         check_command(config)[-3:])
        not_run = check_result(config, 0, '=== tests finished: 1 passed ===\n')
        self.assertFalse(not_run['passed'])
        self.assertEqual('not_confirmed', not_run['selected_case']['status'])
        header = 'sympy/printing/tests/test_pretty.py[1] '
        failure = check_result(config, 1, header + '\ntest_MatrixSymbol_printing F  [FAIL]\n'
                               '=== tests finished: 1 failed ===\n')
        selected = check_result(config, 0, header + '\ntest_MatrixSymbol_printing ok  [OK]\n'
                                '=== tests finished: 1 passed ===\n')
        self.assertTrue(selected['passed'])
        other_file = check_result(config, 0, 'unrelated.py[1]\ntest_MatrixSymbol_printing ok\n'
                                  '=== tests finished: 1 passed ===\n')
        self.assertFalse(other_file['passed'])
        run = Mock(side_effect=[{'passed': False, 'check_status': 'failed'}, failure])
        batch = run_independent_checks(run, '.agent-checks/matadd.py', [target], None)
        self.assertEqual([('.agent-checks/matadd.py',), (target,)],
                         [call.args for call in run.call_args_list])
        self.assertFalse(batch['passed'])
        self.assertEqual('failed', batch['independent_results'][1]['selected_case']['status'])

    def test_environment_errors_cannot_count_as_reproduced_assertion_failures(self):
        config = {'temporary': True}
        assertion = check_result(config, 1, 'Traceback\nAssertionError: wrong output\n')
        missing = check_result(config, 1, "ModuleNotFoundError: No module named 'dependency'\n")
        self.assertEqual('failed', assertion['check_status'])
        self.assertEqual('unknown', missing['check_status'])
        self.assertEqual('unknown', check_result(config, 124, '')['check_status'])
        compared = comparison_result(missing, {'passed': True, 'check_status': 'passed'}, 'regression')
        self.assertEqual('incomplete', compared['status'])

    def test_django_temporary_check_uses_native_settings(self):
        config = {'repo': 'django/django', 'temporary': True, 'target': '.agent-checks/media.py'}
        command = check_command(config)
        self.assertEqual('/tmp/agent-checks/media.py', command[-1])
        self.assertIn('test_sqlite', command[-2])
        self.assertIn('django.setup()', command[-2])
        result = check_result(config, 78, 'Developer check: Django test initialization failed\n')
        self.assertEqual('startup', result['failure_stage'])
        self.assertEqual('INFRA_ERROR', classify_result(None, result)['code'])

    def test_sympy_baseline_comparison_uses_named_failures_and_diagnostics(self):
        config = {'repo': 'sympy/sympy', 'temporary': False}
        old_failure = ('___ sympy/tests/test_example.py:test_old ___\nValueError: old failure\n')
        new_failure = ('___ sympy/tests/test_example.py:test_new ___\nAssertionError: new failure\n')
        old_summary = '=== tests finished: 3 passed, 1 errors ===\n'
        base = check_result(config, 1, old_failure + old_summary)
        self.assertEqual(1, base['counts']['errors'])
        self.assertEqual('BASELINE_FAILURE', classify_result(base, base)['code'])
        different = check_result(config, 1, old_failure.replace('old failure', 'other error') + old_summary)
        self.assertEqual('UNKNOWN', classify_result(base, different)['code'])
        regression = check_result(config, 1, old_failure + new_failure +
                                  '=== tests finished: 2 passed, 1 failed, 1 exceptions ===\n')
        self.assertEqual('REGRESSION', classify_result(base, regression)['code'])
        passed = check_result(config, 0, '=== tests finished: 4 passed ===\n')
        self.assertEqual('PASS', classify_result(base, passed)['code'])
        truncated = check_result(config, 1, old_failure + old_summary, truncated=True)
        self.assertEqual('UNKNOWN', classify_result(base, truncated)['code'])

    def test_summary_failure_cannot_be_hidden_by_a_later_unrelated_passing_check(self):
        progress = RunProgress()
        progress.observe('run_tests', {'ok': True, 'data': {'passed': True,
            'verification': {'level': 'developer_check_failed'}}})
        self.assertTrue(progress.completion_issues('completed', require_check=True))
        self.assertEqual([], progress.completion_issues('incomplete', require_check=True))
        progress.observe('replace_lines', {'ok': True, 'data': {'applied': True}})
        self.assertIsNone(progress.developer_verification)

    def test_base_and_candidate_are_sequential_and_do_not_stage_candidate_on_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = b'candidate patch'
            (root / 'candidate.patch').write_bytes(raw)
            config = {'patch': {'path': 'candidate.patch', 'bytes': len(raw),
                                'sha256': hashlib.sha256(raw).hexdigest()},
                      'checks': [], 'timeout': 90, 'compareBaseline': True, 'purpose': 'regression'}
            (root / 'request.json').write_text(json.dumps(config), encoding='utf-8')
            client = Mock()
            calls = []

            def phase(client_arg, config_arg, directory, files, name, deadline, check_id):
                calls.append(name)
                names = [item[0] for item in files]
                if name == 'baseline':
                    self.assertNotIn('agent.patch', names)
                    return {'passed': False, 'check_status': 'failed'}
                self.assertEqual(['baseline', 'candidate'], calls)
                self.assertIn('agent.patch', names)
                return {'passed': True, 'check_status': 'passed'}

            with patch.dict('sys.modules', {'docker': SimpleNamespace(from_env=Mock(return_value=client))}), \
                    patch('repo_agent.evaluation.check_runner.require_idle'), \
                    patch('repo_agent.evaluation.check_runner.run_phase', side_effect=phase):
                _main(root / 'request.json')
            result = json.loads((root / 'result.json').read_bytes())
            self.assertTrue(result['passed'])
            self.assertFalse(result['baseline']['passed'])
            client.close.assert_called_once()

    def test_phase_removes_container_on_setup_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Mock()
            container = client.containers.create.return_value
            container.id = 'fixture'
            client.images.get.return_value.id = 'image'
            with patch('repo_agent.evaluation.check_runner.execute_setup', side_effect=ValueError('bad source')), \
                    patch('repo_agent.evaluation.check_runner.remove_container') as cleanup:
                with self.assertRaisesRegex(ValueError, 'bad source'):
                    run_phase(client, {'image': 'image'}, Path(temporary), [], 'baseline', 999, 'check')
            cleanup.assert_called_once_with(container, Path(temporary), 'container')
