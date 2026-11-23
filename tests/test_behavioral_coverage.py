import json
from types import SimpleNamespace

from repo_agent.core.agent import Agent
from repo_agent.core.check_execution import check_result, test_command as command
from repo_agent.core.check_quality import delivery_verdict, review_evidence, verification_feedback, verification_summary
from repo_agent.core.check_tools import review_check, run_independent_checks
from repo_agent.core.decisions import Decision
from repo_agent.core.tools import RepoTools


FILE = 'tests/test_format.py'
TARGET = FILE + '::test_nested'
TASK = 'Print nested expressions. For a nested case, expect <nested>.'
SOURCE = 'def test_nested():\n    assert format_value(1) == "<nested>"\n'


def criterion(start=2, **changes):
    return {'requirement': 'Print nested expressions', 'scenario': 'nested formatting',
            'expected': '<nested>', 'expected_basis': 'task', 'dimension': 'composition',
            'start_line': start, 'end_line': start, **changes}


def record(*criteria, target=TARGET, source=SOURCE, reader=None, gaps=(), **updates):
    review = {'purpose': 'control', 'assumptions': '', 'criteria': criteria}
    # The tool boundary serializes a JSON array.
    review['criteria'] = list(review['criteria'])
    review['gaps'] = list(gaps)
    evidence = review_evidence(review, TASK, source, reader, target=target)
    return {'target': target, 'patchSha256': 'patch', 'environmentSha256': 'env',
            'inventorySha256': 'scripts', 'checkId': 'actual-run', 'check_status': 'passed',
            'check_kind': 'existing_repository_tests', 'passed': True,
            'selected_case': {'name': 'test_nested', 'status': 'passed'},
            'reviewEvidence': {**evidence, 'testSha256': 'test'}, **updates}


def coverage(*records, patch='patch', env='env', inventory='scripts'):
    return verification_summary(records, patch, inventory, environment_sha256=env)['behavioralCoverage']


def test_only_the_executed_matching_case_contributes_coverage():
    combined = SOURCE + '\ndef test_other():\n    assert format_value(2) == "<nested>"\n'
    item = record(criterion(), criterion(5, scenario='different nested argument'), source=combined)
    result = coverage(item)
    assert result['counts'] == {'VERIFIED': 1, 'FAILED': 0, 'NOT_COVERED': 1, 'BLOCKED': 0, 'NEEDS_REVIEW': 0}
    assert result['requirementDiscoveryComplete'] is False
    assert result['items'][0]['actual'] == {'status': 'assertion_passed'}
    assert result['items'][0]['evidence'] == {'target': TARGET, 'checkId': 'actual-run',
                                            'testSha256': 'test', 'lines': [2, 2], 'assertionLines': [2]}
    item = coverage(record(criterion(), target=FILE))['items'][0]
    assert item['status'] == 'NEEDS_REVIEW'
    assert item['execution']['granularity'] == 'check'
    assert item['actual']['status'] == 'check_passed'
    assert coverage()['status'] == 'NOT_COVERED'


def test_invalid_or_unexecuted_assertions_cannot_be_verified():
    conditional = 'def test_nested():\n    if False:\n        assert format_value(1) == "<nested>"\n'
    early = 'def test_nested():\n    if flag:\n        return\n    assert format_value(1) == "<nested>"\n'
    for item in (record(criterion(3), source=conditional), record(criterion(4), source=early),
                 record(criterion(requirement='Invented task quote')),
                 record(criterion(), source=SOURCE.replace('format_value(1)', '"<nested>"')),
                 record(criterion(), source=SOURCE.replace('"<nested>"', '"other"')),
                 record(criterion(), selected_case={'name': 'test_nested', 'status': 'not_confirmed'})):
        assert coverage(item)['counts']['VERIFIED'] == 0


def test_one_assertion_cannot_confirm_multiple_declared_scenarios():
    result = coverage(record(criterion(), criterion(scenario='different composition')))
    assert result['counts']['NOT_COVERED'] == 2
    assert result['counts']['VERIFIED'] == 0


def test_oracle_conflict_preserves_both_citations_and_observed_pass():
    frozen = 'assert format_value(1) == "old"\n'
    cited = {'path': FILE, 'start_line': 1, 'end_line': 1, 'quote': frozen.strip()}
    declaration = criterion(evidence=[cited], oracle_conflict={
        'reference': 1, 'assessment': 'Compatibility depends on which callers receive the nested value.'})
    item = record(declaration, reader=lambda path: {'text': frozen, 'sha256': 'old'})
    result = coverage(item)['items'][0]
    assert result['status'] == 'BLOCKED'
    assert result['actual'] == {'status': 'assertion_passed'}
    assert result['oracleConflict']['repository']['source_sha256'] == 'old'
    assert result['reason'] == 'oracle_conflict_unresolved'
    assert coverage(record(declaration))['items'][0]['reason'] == 'oracle_citation_unverified'


def test_only_identified_failed_assertion_is_failed_and_unavailable_is_blocked():
    failure = {'passed': False, 'check_status': 'failed', 'failure_stage': 'assertion',
               'diagnostic': {'location': {'path': '/testbed/' + FILE, 'line': 2},
                              'values': {'status': 'unavailable'}}}
    source = SOURCE + '    assert format_value(2) == "<nested>"\n'
    results = coverage(record(criterion(), criterion(3), source=source, **failure))['items']
    assert [item['status'] for item in results] == ['FAILED', 'NEEDS_REVIEW']
    assert results[0]['actual'] == {'status': 'assertion_failed', 'values': {'status': 'unavailable'}}
    assert coverage(record(criterion(), check_status='unknown', passed=False))['status'] == 'BLOCKED'
    assert coverage(record(criterion(), check_status='not_run', passed=False))['status'] == 'BLOCKED'
    scratch = record(criterion(expected_basis='assumption'), target='.agent-checks/test_format.py',
                     check_kind='model_authored_python',
                     failure_evidence=['.agent-checks/test_format.py:2 | AssertionError'], **failure)
    assert coverage(scratch)['items'][0]['actual']['status'] == 'assertion_failed'
    assert coverage(scratch)['status'] == 'BLOCKED'
    assert coverage({**scratch, 'failure_evidence': []})['status'] == 'NEEDS_REVIEW'


def test_prose_oracle_preserves_observed_assertion_and_requires_literal_source_to_verify():
    prose = criterion(expected='Returns the nested text specified by the task.')
    item = coverage(record(prose))['items'][0]
    assert item['status'] == 'NEEDS_REVIEW'
    assert item['actual']['status'] == 'assertion_passed'
    assert item['oracle']['status'] == 'unconfirmed'
    assert item['mapping'] == 'referenced'
    assert coverage(record({**prose, 'expected_quote': '<nested>'}))['status'] == 'VERIFIED'


def test_declared_gap_survives_edits_and_closes_only_on_matching_current_evidence():
    gap = {'id': 'nested', 'requirement': 'Print nested expressions', 'scenario': 'nested formatting',
           'verification_goal': 'Check the output for nested content.'}
    first = record(criterion(), target=FILE, gaps=[gap, {**gap, 'id': 'lower'}])
    later = record(criterion(gap_id='nested'), patchSha256='updated')
    summary = verification_summary([first, later], 'patch', 'scripts')
    assert summary['priorityGap'] == gap
    verdict = delivery_verdict('agent_reported_finished', 'completed', summary)
    assert verdict['model_reported_outcome'] == 'completed'
    assert verdict['acceptance_status'] == 'needs_review'
    assert len(verdict['remaining_gaps']) == 2
    assert 'remainingGaps' not in verification_feedback(summary, FILE)
    resolved = verification_summary([first, later], 'updated', 'scripts')
    assert resolved['priorityGap']['id'] == 'lower'
    stale = verification_summary([first, later], 'another', 'scripts')
    assert stale['priorityGap'] == gap


def test_batch_feedback_keeps_each_targets_scenarios():
    first = record(criterion(), target=FILE)
    last = record(criterion())
    summary = verification_summary([first, last], 'patch', 'scripts')
    def run(target):
        return {'passed': True, 'check_status': 'passed', 'verification': verification_feedback(summary, target)}
    batch = run_independent_checks(run, FILE, [TARGET], None)['verification']['behavioralCoverage']
    assert 'currentCheck' not in batch
    assert [group['scenarios'][0]['status'] for group in batch['byTarget']] == ['NEEDS_REVIEW', 'VERIFIED']


def test_model_can_finish_with_a_passing_check_and_an_unresolved_gap(tmp_path):
    gap = {'id': 'nested', 'requirement': 'Print nested expressions', 'scenario': 'nested formatting',
           'verification_goal': 'Check the output for nested content.'}
    checked = record(criterion(), target=FILE, gaps=[gap])
    summary = verification_summary([checked], 'patch', 'scripts')
    choices = iter([Decision('run_tests', {'target': FILE}),
                    Decision('finish', {'answer': 'Finished the checks.', 'outcome': 'completed'})])
    model = SimpleNamespace(decide=lambda messages: next(choices))
    tools = SimpleNamespace(root=tmp_path, require_check_before_completion=True,
                            execute=lambda action, args: {'ok': True, 'data': {
                                **checked, 'verification': verification_feedback(summary, FILE)}})
    result = Agent(model, tools, trace_dir=tmp_path / 'trace').run(TASK)
    assert result['steps'] == 2
    assert result['execution_status'] == 'agent_reported_finished'
    assert result['model_reported_outcome'] == 'completed'
    assert result['acceptance_status'] == 'needs_review'
    assert result['remaining_gaps'] == [gap]


def test_multi_failure_diagnostics_remain_partial_when_a_traceback_is_missing():
    output = ('ERROR: test_warning\nTraceback (most recent call last):\n'
              '  File "/tmp/agent-checks/example.py", line 12\n    get_media()\n'
              'django.forms.widgets.MediaOrderConflictWarning: conflicting assets\n'
              'ERROR: test_missing_traceback\nRuntimeError: missing context\n'
              'FAIL: test_order\nTraceback (most recent call last):\n'
              '  File "/tmp/agent-checks/example.py", line 22\n    assert order == expected\n'
              'AssertionError: wrong order\nFAILED (failures=1, errors=2)\n')
    result = check_result({'temporary': True, 'repo': 'django/django'}, 1, output)
    assert [(failure['kind'], (failure['diagnostic']['location'] or {}).get('line'))
            for failure in result['failure_records']] == [
        ('behavior_warning', 12), ('runtime_exception', None), ('assertion_failure', 22)]
    assert result['failure_evidence_complete'] is False


def test_existing_p1_freshness_controls_coverage():
    item = record(criterion())
    assert coverage(item, inventory='changed')['status'] == 'VERIFIED'
    assert coverage(item, patch='new')['items'] == []
    assert coverage(item, env='other')['items'] == []
    assert coverage(item, {**item, 'check_status': 'unknown', 'passed': False})['status'] == 'BLOCKED'


def test_feedback_keeps_current_verdict_and_leaves_a_complete_saved_record():
    item = record(criterion())
    summary = verification_summary([item], 'patch', 'scripts', environment_sha256='env')
    feedback = verification_feedback(summary, TARGET)['behavioralCoverage']
    assert feedback['counts']['VERIFIED'] == 1
    assert feedback['currentCheck'] == [{'criterion': 1, 'status': 'VERIFIED',
                                         'reason': 'selected_case_passed', 'observed': 'assertion_passed',
                                         'granularity': 'assertion', 'oracle': 'source_supported'}]
    assert 'items' not in feedback and 'items' in summary['behavioralCoverage']
    assert 'currentCheck' not in verification_feedback(summary)['behavioralCoverage']


def test_django_named_case_needs_exact_runner_identity():
    config = {'repo': 'django/django', 'temporary': False,
              'target': 'tests/forms_tests/tests/test_media.py::MediaTests::test_construction'}
    assert '--verbosity' in command(config)
    summary = 'Ran 1 test in 0.001s\nOK\n'
    for owner in ('MediaTests', 'MediaTests.test_construction'):
        output = f'test_construction (forms_tests.tests.test_media.{owner}) ... ok\n' + summary
        assert check_result(config, 0, output)['selected_case']['status'] == 'passed'
    assert check_result(config, 0, summary)['selected_case']['status'] == 'not_confirmed'
    same_name = 'test_construction (other.MediaTests) ... ok\n' + summary
    assert check_result(config, 0, same_name)['selected_case']['status'] == 'not_confirmed'


def test_local_pytest_confirms_exact_case_and_rejects_skip(tmp_path):
    path = tmp_path / 'test_format.py'
    path.write_text('import pytest\n\n' + SOURCE.replace('format_value(1)', 'str("<nested>")')
                    + '\n@pytest.mark.skip\ndef test_skip():\n    assert False\n', encoding='utf-8')
    tools = RepoTools(tmp_path, allow_tests=True, test_env={'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}, test_timeout=20)
    target = 'test_format.py::test_nested'
    passed = tools.run_tests(target)
    assert passed['passed'] is True, json.dumps(passed)
    assert passed['selected_case'] == {'name': 'test_nested', 'status': 'passed'}
    review = {'purpose': 'control', 'assumptions': '', 'criteria': [criterion(4)]}
    _, evidence = review_check({}, target, path.read_bytes(), review, TASK, None)
    assert coverage({**record(criterion()), **passed, 'target': target, 'reviewEvidence': evidence})['status'] == 'VERIFIED'
    skipped = tools.run_tests('test_format.py::test_skip')
    assert skipped['selected_case']['status'] == 'not_confirmed'
