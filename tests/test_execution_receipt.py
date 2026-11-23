import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from repo_agent.core.check_execution import classify_result, execution_receipt, interpret_execution, validate_check_argv
from repo_agent.core.check_quality import assertion_observed
from repo_agent.core.tools import RepoTools
from repo_agent.evaluation.check_runner import run_phase


TARGET = 'tests/test_example.py::test_case'
CONFIG = {'repo': 'pytest', 'target': TARGET, 'temporary': False, 'report': '/report.xml'}
CASE = {'case': 'test_case', 'line': 2, 'endLine': 2}


def report(*, failure='', tests=1, skipped=0):
    if tests:
        case = f'<testcase classname="tests.test_example" name="test_case">{failure}</testcase>'
    else:
        case = ''
    return (f'<testsuite tests="{tests}" failures="{int("<failure" in failure)}" errors="0" '
            f'skipped="{skipped}">{case}</testsuite>').encode()


def interpret(output='', *, code=0, xml=None, truncated=False):
    receipt = execution_receipt(['test-runner', TARGET], '/workspace', exit_code=code)
    return interpret_execution(CONFIG, receipt, output, truncated=truncated, report=xml)


def test_successful_exit_with_unrecognized_summary_preserves_facts():
    result = interpret('73 passed, 1 xfailed in 17.94 seconds')
    assert result['execution']['status'] == 'exited'
    assert result['execution']['exit_code'] == 0
    assert result['check_status'] == 'unknown'
    assert result['test_evidence'] == {'source': 'execution', 'granularity': 'none', 'report_status': 'missing'}
    assert assertion_observed(result, CASE) is None


def test_machine_report_can_confirm_the_named_case_without_text():
    result = interpret('Report written', xml=report())
    assert result['passed'] and result['test_evidence']['source'] == 'junit'
    assert assertion_observed(result, CASE) == {'status': 'assertion_passed'}


@pytest.mark.parametrize('body, call, code, status', [
    ('assert False', '', 0, 'unknown'),
    ('assert False', '\ntest_case()\n', 1, 'failed'),
    ('assert True', '\ntest_case()\n', 0, 'unknown'),
])
def test_direct_python_uses_receipt_for_exit_and_traceback_for_failure(tmp_path, body, call, code, status):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    (scratch / 'sample.py').write_text(f'def test_case():\n    {body}\n{call}', encoding='utf-8')
    tools = RepoTools(tmp_path, allow_tests=True, test_log_dir=tmp_path.parent / f'{tmp_path.name}-logs')
    result = tools.run_tests('.agent-checks/sample.py', scratch=scratch)
    assert result['execution']['status'] == 'exited'
    assert result['execution']['exit_code'] == code
    assert result['execution']['logs']['status'] == 'complete'
    assert result['check_status'] == status
    assert not result['passed']
    if code:
        assert result['failure_stage'] == 'assertion'
        assert result['failure_evidence_complete']
        assert result['failure_evidence'] == ['.agent-checks/sample.py:2 | AssertionError']
    else:
        assert result['check_reason'] == 'TEST_EVIDENCE_UNAVAILABLE'
        assert result['test_evidence']['granularity'] == 'none'
        assert classify_result(None, result)['code'] == 'UNKNOWN'


def test_local_test_runner_confirms_a_real_named_case(tmp_path):
    test_file = tmp_path / 'tests' / 'test_example.py'
    test_file.parent.mkdir()
    test_file.write_text('def test_case():\n    assert str(9) == "9"\n', encoding='utf-8')
    tools = RepoTools(tmp_path, allow_tests=True, test_junit=True,
                      test_env={'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'})
    result = tools.run_tests(TARGET)
    assert result['passed']
    assert result['test_evidence'] == {'source': 'junit', 'granularity': 'case', 'report_status': 'valid'}
    assert assertion_observed(result, CASE) == {'status': 'assertion_passed'}


def test_parser_exception_keeps_receipt_and_allows_machine_evidence():
    with patch('repo_agent.core.check_execution.check_result',
               side_effect=[ValueError('bad summary'), {'passed': False, 'failure_detail': None,
                                                       'failure_kind': 'not_executed'}]):
        result = interpret('Invalid runner output', xml=report())
    assert result['execution']['exit_code'] == 0
    assert result['passed'] and result['test_evidence']['source'] == 'junit'
    assert result['test_evidence']['text_status'] == 'invalid'


def test_invalid_machine_report_can_fall_back_to_recognized_text():
    result = interpret('1 passed in 0.1s\n', xml=b'<bad')
    assert result['passed']
    assert result['test_evidence']['report_status'] == 'invalid'
    assert result['test_evidence']['source'] == 'text'
    assert assertion_observed(result, CASE) is None


@pytest.mark.parametrize('facts', [
    {'truncated': True, 'output': '1 passed in 0.1s'},
    {'xml': report(tests=0)},
    {'xml': report(failure='<skipped/>', skipped=1)},
    {'xml': report(failure='<failure/>')},
    {'xml': report(), 'code': 1},
    {'xml': b'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase><failure/></testcase></testsuite>'},
])
def test_incomplete_or_inconsistent_evidence_cannot_confirm_case(facts):
    result = interpret(**facts)
    assert not result['passed']
    assert assertion_observed(result, CASE) is None


def test_conflicting_report_does_not_change_exit_code():
    result = interpret(xml=report(failure='<failure/>'))
    assert result['execution']['exit_code'] == 0
    assert result['test_evidence']['report_status'] == 'conflict'


def test_internally_conflicting_report_cannot_be_masked_by_passing_text():
    output = TARGET + ' PASSED [100%]\n1 passed in 0.1s\n'
    assert assertion_observed(interpret(output), CASE) == {'status': 'assertion_passed'}
    xml = b'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase><failure/></testcase></testsuite>'
    result = interpret(output, xml=xml)
    assert result['test_evidence']['report_status'] == 'conflict'
    assert assertion_observed(result, CASE) is None


def test_host_and_container_preserve_same_facts_for_custom_command(tmp_path):
    target = 'run-test.py'
    (tmp_path / target).write_text('import sys\nprint("custom result")\nprint("diagnostic", file=sys.stderr)\n')
    local = RepoTools(tmp_path, allow_tests=True, test_argv=[sys.executable, '{target}'],
                      test_log_dir=tmp_path.parent / f'{tmp_path.name}-logs').run_tests(target)
    assert local['execution']['logs']['status'] == 'complete'
    stdout = local['execution']['logs']['stdout']
    content = Path(stdout['path']).read_bytes()
    assert content.strip() == b'custom result'
    assert stdout['sha256'] == hashlib.sha256(content).hexdigest()

    folder = tmp_path / 'container-run'
    folder.mkdir()
    client = Mock()
    client.images.get.return_value.id = 'image'
    container = client.containers.create.return_value
    container.id = 'container'
    container.put_archive.return_value = True
    container.exec_run.side_effect = [
        SimpleNamespace(exit_code=0), SimpleNamespace(exit_code=0, output=b'custom result\ndiagnostic\n'),
    ]
    config = {'repo': 'example/new-repo', 'argv': ['native-test', '{target}'], 'target': target,
              'temporary': False, 'image': 'image', 'baseCommit': 'base', 'tree': 'tree',
              'patch': {'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest()}}
    with patch('repo_agent.evaluation.check_runner.execute_setup', side_effect=['base', 'tree', '', '', '']), \
            patch('repo_agent.evaluation.check_runner.archive_check_output', return_value={
                'status': 'archived', 'path': 'output.tar'}), \
            patch('repo_agent.evaluation.check_runner.remove_container') as cleanup:
        remote = run_phase(client, config, folder, [], 'candidate', float('inf'), 'check')
    cleanup.assert_called_once()
    assert remote['execution']['argv'] == ['native-test', target]
    assert remote['execution']['logs']['combined']['path'] == 'test-output.txt'
    assert json.loads((folder / 'execution.json').read_bytes()) == remote['execution']
    assert remote['execution']['status'] == local['execution']['status'] == 'exited'
    assert remote['exit_code'] == local['exit_code'] == 0
    assert remote['check_status'] == local['check_status'] == 'unknown'
    assert remote['test_evidence'] == local['test_evidence']


def test_invalid_command_cannot_replace_authorized_target():
    with pytest.raises(ValueError):
        validate_check_argv(['test-command', 'another-target'])
