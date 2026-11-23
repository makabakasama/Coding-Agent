"""Coding check evidence summaries. Model coverage remains self-reported."""
import ast
from collections import Counter
from copy import deepcopy
import hashlib
import json

from repo_agent.core.decisions import CHECK_REVIEW_SCHEMA, _validate_value


def selected_scope(tree, target):
    if tree is None:
        return None
    _, *selectors = target.split('::')
    scope = tree
    for name in selectors:
        matches = [node for node in scope.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == name]
        if len(matches) != 1:
            return None
        scope = matches[0]
    return scope


def assertion_site(scope, target, criterion):
    """Recognize a small subset of assertions with a source-visible expected value."""
    _, *selectors = target.split('::')
    start, end = criterion['start_line'], criterion['end_line']
    matches = []
    for node in ast.walk(scope):
        if not (start <= getattr(node, 'lineno', 0) and getattr(node, 'end_lineno', end + 1) <= end):
            continue
        expression = node.test if isinstance(node, ast.Assert) else None
        call = node.value if isinstance(node, ast.Expr) else None
        if (isinstance(expression, ast.Compare) and len(expression.ops) == 1
                and isinstance(expression.ops[0], (ast.Eq, ast.Is))):
            actual, expected = expression.left, expression.comparators[0]
        elif (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
              and isinstance(call.func.value, ast.Name) and call.func.value.id == 'self'
              and call.func.attr in {'assertEqual', 'assertIs', 'assertListEqual', 'assertTupleEqual'}
              and len(call.args) >= 2):
            actual, expected = call.args[:2]
        else:
            continue
        expectation_matches = False
        try:
            ast.literal_eval(actual)
        except (ValueError, TypeError):
            try:
                value = ast.literal_eval(expected)
            except (ValueError, TypeError):
                pass
            else:
                expectation_matches = criterion.get('expected_quote', criterion['expected']).strip() in (repr(value), value)
        matches.append((node, expectation_matches))
    if len(matches) != 1:
        return None
    node, expectation_matches = matches[0]
    direct = isinstance(scope, ast.FunctionDef) and node in scope.body and not scope.decorator_list
    if direct:
        # A named case may pass via an early return before the cited assertion.
        direct = not any(isinstance(part, ast.Return) for statement in scope.body[:scope.body.index(node)]
                         for part in ast.walk(statement))
        direct = direct and not any(isinstance(part, (ast.Yield, ast.YieldFrom)) for part in ast.walk(scope))
    return {'line': node.lineno, 'endLine': node.end_lineno,
            'expectationMatched': expectation_matches,
            'case': '::'.join(selectors) if direct and selectors else None}


def review_evidence(review, problem, test_text, repository_reader=None, *, target=None):
    if review is None:
        return {'source': 'not_provided', 'references_valid': False,
                'warnings': ['No requirement-to-test review was supplied'],
                'unresolved_assumptions': []}
    _validate_value(CHECK_REVIEW_SCHEMA, review, 'review')
    warnings = []
    unresolved = []
    problem_text = ' '.join(problem.split())
    test_lines = test_text.splitlines()
    checked = deepcopy(review)
    criterion_checks = []
    try:
        tree = ast.parse(test_text) if target and target.split('::', 1)[0].endswith('.py') else None
    except (SyntaxError, ValueError, RecursionError):
        tree = None
    scope = selected_scope(tree, target)
    source_assertions = [(node.lineno, node.end_lineno) for node in ast.walk(scope)
                         if isinstance(node, ast.Assert) or (
                             isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                             and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self'
                             and node.func.attr.startswith('assert'))] if scope is not None else []
    for index, criterion in enumerate(checked['criteria'], 1):
        first_warning = len(warnings)
        requirement = ' '.join(criterion['requirement'].split())
        task_quote_valid = bool(requirement and requirement in problem_text)
        if not task_quote_valid:
            warnings.append(f'Criterion {index} does not quote the supplied task')
        start, end = criterion['start_line'], criterion['end_line']
        lines_valid = 1 <= start <= end <= len(test_lines) and any(line.strip() for line in test_lines[start - 1:end])
        if not lines_valid:
            warnings.append(f'Criterion {index} does not reference existing nonempty test lines')

        repository_evidence = []
        for reference in criterion.get('evidence', []):
            verified = False
            if repository_reader is not None:
                try:
                    source = repository_reader(reference['path'])
                    lines = source['text'].splitlines()
                    ref_start, ref_end = reference['start_line'], reference['end_line']
                    excerpt = '\n'.join(lines[ref_start - 1:ref_end]) if 1 <= ref_start <= ref_end <= len(lines) else ''
                    verified = (bool(excerpt) and reference['quote'] in excerpt)
                    reference['source_sha256'] = source['sha256']
                except (OSError, ValueError, KeyError):
                    verified = False
            if not verified:
                warnings.append(f'Criterion {index} has repository evidence that could not be verified')
            repository_evidence.append({**reference, 'verified': verified})
        criterion['evidence'] = repository_evidence

        basis = criterion['expected_basis']
        expected = ' '.join(criterion.get('expected_quote', criterion['expected']).split())
        oracle = 'assumed' if basis == 'assumption' else 'unconfirmed'
        if basis == 'task' and expected and expected in problem_text:
            oracle = 'source_supported'
        elif basis == 'task':
            warnings.append(f'Criterion {index} expected result is not stated in the task')
        elif basis == 'repository':
            if expected and any(expected in ' '.join(item['quote'].split()) for item in repository_evidence if item['verified']):
                oracle = 'source_supported'
            else:
                warnings.append(f'Criterion {index} expected result is not supported by its repository quote')
        elif basis == 'assumption':
            unresolved.append({'criterion': index, 'expected': criterion['expected']})

        conflict = criterion.get('oracle_conflict')
        conflict_reference = None
        if conflict:
            oracle = 'conflict_unresolved'
            position = conflict['reference'] - 1
            if task_quote_valid and position < len(repository_evidence) and repository_evidence[position]['verified']:
                conflict_reference = position + 1
            else:
                warnings.append(f'Criterion {index} has an oracle conflict without verified task and repository citations')
        criterion_checks.append({'referencesValid': len(warnings) == first_warning,
                                 'mappingValid': task_quote_valid and lines_valid,
                                 'oracle': oracle,
                                 'sourceAssertionLines': sorted({line for line, last in source_assertions
                                                                 if start <= line <= last <= end})[:12],
                                 'assertion': assertion_site(scope, target, criterion) if scope is not None else None,
                                 **({'oracleReference': conflict_reference} if conflict else {})})

    sites = Counter(check['assertion']['line'] for check in criterion_checks if check['assertion'])
    for check in criterion_checks:
        if check['assertion'] and sites[check['assertion']['line']] > 1:
            check['sharedAssertion'] = True
    valid_gaps = [gap for gap in checked.get('gaps', [])
                  if ' '.join(gap['requirement'].split()) and ' '.join(gap['requirement'].split()) in problem_text]
    if len(valid_gaps) != len(checked.get('gaps', [])):
        warnings.append('Some gaps do not quote the supplied task')
    return {'source': 'model_reported', 'review': checked,
            'references_valid': not warnings, 'semanticCoverageVerified': False,
            'warnings': warnings, 'unresolved_assumptions': unresolved,
            'criterionChecks': criterion_checks, 'declaredGaps': valid_gaps}


def evidence_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def assertion_observed(record, site):
    if not site:
        return None
    status = record.get('check_status')
    selected = record.get('selected_case') or {}
    if (site['case'] and status == 'passed' and record.get('passed') is True
            and selected.get('name') == site['case'] and selected.get('status') == 'passed'):
        return {'status': 'assertion_passed'}
    failures = record.get('failure_records') or []
    if not failures and status == 'failed' and record.get('failure_stage') == 'assertion':
        failures = [{'kind': 'assertion_failure', 'diagnostic': record.get('diagnostic')}]
    if record.get('failure_stage') not in {'assertion', 'test_execution'}:
        return None
    for failure in failures:
        if failure['kind'] != 'assertion_failure':
            continue
        diagnostic = failure.get('diagnostic') or {}
        location = diagnostic.get('location') or {}
        path = location.get('path', '').replace('\\', '/')
        target = record['target'].split('::', 1)[0].removeprefix('.agent-checks/')
        # Temporary checks require P1's identified scratch failure, not any same-basename frame.
        if record.get('check_kind') == 'model_authored_python':
            line = location.get('line')
            at_target = any(item.startswith(f'.agent-checks/{target}:{line} | ')
                            for item in record.get('failure_evidence', []))
        else:
            at_target = path == target or path.endswith('/' + target)
        if at_target and site['line'] <= location.get('line', 0) <= site['endLine']:
            return {'status': 'assertion_failed', 'values': diagnostic.get('values', {'status': 'unavailable'})}
    return None


def behavior_outcome(record, criterion, checked, observed):
    if criterion.get('oracle_conflict'):
        return 'BLOCKED', 'oracle_conflict_unresolved' if checked.get('oracleReference') else 'oracle_citation_unverified'
    if not checked.get('mappingValid'):
        return 'NOT_COVERED', 'mapping_unverified'
    if not observed and (record.get('check_status') in {'unknown', 'not_run'} or record.get('failure_stage') == 'candidate_syntax'):
        return 'BLOCKED', 'test_result_unavailable'
    if checked.get('sharedAssertion'):
        return 'NOT_COVERED', 'assertion_reused_for_multiple_scenarios'
    if observed and criterion['expected_basis'] == 'assumption':
        return 'BLOCKED', 'expected_behavior_unconfirmed'
    if observed and checked['oracle'] == 'source_supported' and checked['assertion']['expectationMatched']:
        return ('VERIFIED', 'selected_case_passed') if observed == 'assertion_passed' else ('FAILED', 'mapped_assertion_failed')
    if observed or checked.get('sourceAssertionLines') and record.get('check_status') in {'passed', 'failed'}:
        if checked['oracle'] == 'unconfirmed':
            return 'NEEDS_REVIEW', 'oracle_unconfirmed'
        return 'NEEDS_REVIEW', 'assertion_expectation_link_unconfirmed' if observed else 'evidence_granularity_insufficient'
    return 'NOT_COVERED', 'no_individual_assertion_result'


def behavioral_coverage(records):
    items = []
    for record in records:
        evidence = record.get('reviewEvidence', {})
        criteria = evidence.get('review', {}).get('criteria', [])
        assessments = evidence.get('criterionChecks', [])
        for index, criterion in enumerate(criteria, 1):
            checked = assessments[index - 1] if index <= len(assessments) else {}
            observed = assertion_observed(record, checked.get('assertion'))
            status, reason = behavior_outcome(record, criterion, checked, observed['status'] if observed else None)
            check_status = record.get('check_status')
            stage = record.get('failure_stage')
            executed = stage == 'test_execution' or (check_status in {'passed', 'failed'} and stage != 'candidate_syntax')
            check_result = 'failed' if stage == 'test_execution' else check_status
            execution = {'status': 'executed' if executed else 'not_executed'
                         if check_status == 'not_run' or record.get('failure_stage') in {'candidate_syntax', 'startup'} else 'unknown',
                         'result': check_result if executed else 'unavailable',
                         'granularity': 'assertion' if observed else 'check' if executed else 'unavailable'}
            actual = observed or {'status': f'check_{check_result}' if executed else 'unavailable'}
            item = {'criterion': index, 'requirement': criterion['requirement'], 'scenario': criterion['scenario'],
                    'dimension': criterion.get('dimension', 'example'), 'expected': criterion['expected'],
                    'status': status, 'reason': reason, 'actual': actual, 'execution': execution,
                    'mapping': 'referenced' if checked.get('mappingValid') else 'unconfirmed',
                    'oracle': {'status': checked.get('oracle', 'unconfirmed'), 'basis': criterion['expected_basis']},
                    'evidence': {'target': record['target'], 'checkId': record.get('checkId'),
                                 'testSha256': evidence.get('testSha256'),
                                 'assertionLines': checked.get('sourceAssertionLines', []),
                                 'lines': [criterion['start_line'], criterion['end_line']]}}
            if criterion.get('gap_id'):
                item['gapId'] = criterion['gap_id']
            if conflict := criterion.get('oracle_conflict'):
                reference = checked.get('oracleReference')
                item['oracleConflict'] = {'status': 'unresolved', 'assessment': conflict['assessment'],
                                          'repository': criterion['evidence'][reference - 1] if reference else None}
            items.append(item)
    counts = {status: sum(item['status'] == status for item in items)
              for status in ('VERIFIED', 'FAILED', 'NOT_COVERED', 'BLOCKED', 'NEEDS_REVIEW')}
    return {'scope': 'declared_scenarios', 'requirementDiscoveryComplete': False,
            'status': next((status for status in ('FAILED', 'BLOCKED', 'NOT_COVERED', 'NEEDS_REVIEW', 'VERIFIED') if counts[status]),
                           'NOT_COVERED'), 'counts': counts, 'items': items}


def verification_feedback(summary, target=None):
    """Keep full coverage in the saved report and send verdicts for the current check."""
    coverage = summary['behavioralCoverage']
    outcomes = [{'criterion': item['criterion'], 'status': item['status'], 'reason': item['reason'],
                 'observed': item['actual']['status'], 'granularity': item['execution']['granularity'],
                 'oracle': item['oracle']['status']}
                for item in coverage['items'] if item['evidence']['target'] == target]
    return {**{key: value for key, value in summary.items() if key != 'remainingGaps'}, 'behavioralCoverage': {
        **{key: value for key, value in coverage.items() if key != 'items'},
        **({'currentCheck': outcomes} if outcomes else {}),
    }}


def delivery_verdict(execution_status, model_outcome, verification, *, assess=True):
    priority = verification.get('priorityGap')
    return {'execution_status': execution_status, 'model_reported_outcome': model_outcome,
            'acceptance_status': 'needs_review' if assess else 'not_assessed',
            'remaining_gaps': verification.get('remainingGaps', [priority] if priority else []),
            'priority_gap': priority}


def verification_summary(records, patch_sha256, inventory_sha256, *, environment_sha256=None):
    # Keep the latest outcome for each target, including an infrastructure error.
    latest = {}
    for item in records:
        latest[item['target']] = item
    current = [item for item in latest.values()
               if item.get('patchSha256') == patch_sha256
               and (environment_sha256 is None or item.get('environmentSha256') == environment_sha256)
               and (item.get('check_kind') == 'existing_repository_tests'
                    or item.get('inventorySha256') == inventory_sha256)]
    level = 'needs_review'
    if not current or any(item.get('check_status') == 'unknown' for item in current):
        level = 'verification_incomplete'
    elif any(item.get('passed') is False for item in current):
        level = 'developer_check_failed'
    elif any(item.get('comparison', {}).get('status') == 'incomplete' for item in current):
        level = 'verification_incomplete'
    regression = [item for item in current if item.get('check_kind') == 'existing_repository_tests']
    reasons = []
    if not current:
        reasons.append('No current developer checks')
    if len(current) < len(latest):
        reasons.append('Some check records are stale')
    if not regression:
        reasons.append('Related existing repository tests have not been run on this version')
    if any(not item.get('reviewEvidence', {}).get('references_valid') for item in current):
        reasons.append('Requirement-to-test review is missing or has invalid references')
    if any(item.get('reviewEvidence', {}).get('unresolved_assumptions') for item in current):
        reasons.append('Some test expectations remain model assumptions')
    for item in current:
        classification = item.get('classification', {})
        if classification.get('code') in {'BASELINE_FAILURE', 'INFRA_ERROR', 'REGRESSION', 'UNKNOWN'}:
            reasons.append(f'{item["target"]} [{classification["code"]}] {classification["reason"]}')
    reasons.append('Developer checks and model-reported coverage require external review')
    coverage = behavioral_coverage(current)
    declared = {(gap['id'], gap['requirement'], gap['scenario']): gap
                for item in records for gap in item.get('reviewEvidence', {}).get('declaredGaps', [])}
    resolved = {(item.get('gapId'), item['requirement'], item['scenario'])
                for item in coverage['items'] if item['status'] == 'VERIFIED'}
    gaps = [gap for key, gap in declared.items() if key not in resolved]
    return {'level': level, 'officialGrade': 'not_run', 'semanticCoverageVerified': False,
            'behavioralCoverage': coverage, 'remainingGaps': gaps, 'priorityGap': gaps[0] if gaps else None,
            'patchSha256': patch_sha256, 'reasons': reasons,
            'environmentSha256': environment_sha256,
            'regressionScope': 'selected_repository_test_files',
            'repositoryRegression': 'passed' if regression and all(item.get('passed') for item in regression)
                                    else 'failed_or_unknown' if regression else 'not_run',
            'checks': [{'target': item['target'], 'checkId': item.get('checkId'),
                        'status': item.get('check_status'),
                        'checkKind': item.get('check_kind'),
                        'testSha256': item.get('reviewEvidence', {}).get('testSha256'),
                        'environmentSha256': item.get('environmentSha256'),
                        'classification': item.get('classification', {}).get('code'),
                        'comparison': item.get('comparison', {}).get('status')}
                       for item in current]}
