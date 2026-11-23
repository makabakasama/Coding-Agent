"""Run the existing coding Agent in a prepared disposable workspace."""
import json
import importlib.metadata
from pathlib import Path
import time

from repo_agent.core.agent import Agent
from repo_agent.core.check_quality import delivery_verdict
from repo_agent.core.coding_budget import BudgetedCodingModel as _BudgetedModel
from repo_agent.core.provider import OpenAICompatibleProvider
from repo_agent.core.token_budget import TokenBudget
from repo_agent.core.progress import CHECK_REMINDER_ACTIONS
from repo_agent.backend.services.task_execution_tools import TaskRepoTools
from repo_agent.evaluation.artifacts import digest, load_run, read_reference, save
from repo_agent.evaluation.swebench import git
from repo_agent.evaluation.test_targets import FrozenTestTargets
from repo_agent.infrastructure.redaction import Redactor
from repo_agent.infrastructure.run_evidence import archive_agent_source


class _RecordedAgent(Agent):
    def _record(self, item):
        super()._record(self.redactor.private_value(item))


def _baseline(directory, run, task, path):
    """Require external evidence of target failures plus passing controls before real calls."""
    raw = Path(path).read_bytes()
    report = json.loads(raw)
    if not isinstance(report, dict):
        raise ValueError('Baseline must be an official per-instance report object')
    item = report.get(task['taskId'], {})
    source = json.loads(read_reference(directory, run['gradingInput']))[0]
    tests = item.get('tests_status') or {}
    f2p, p2p = tests.get('FAIL_TO_PASS', {}), tests.get('PASS_TO_PASS', {})
    expected_f2p = source['FAIL_TO_PASS']
    expected_p2p = source['PASS_TO_PASS']
    expected_f2p = json.loads(expected_f2p) if isinstance(expected_f2p, str) else expected_f2p
    expected_p2p = json.loads(expected_p2p) if isinstance(expected_p2p, str) else expected_p2p
    if (item.get('resolved') is not False or item.get('patch_exists') is not True
            or item.get('patch_successfully_applied') is not True
            or item.get('infra_failure', False) is not False
            or not expected_f2p or not f2p.get('failure')
            or not set(expected_f2p) <= set(f2p.get('success', []) + f2p.get('failure', []))
            or p2p.get('failure') or not set(expected_p2p) <= set(p2p.get('success', []))):
        raise ValueError('Baseline must show target test failures and passing controls, not an environment failure')
    if Redactor().private(raw.decode('utf-8')) != raw.decode('utf-8'):
        raise ValueError('Known credential in supplied baseline report')
    output = Path(path).with_name('test_output.txt')
    if output.is_file():
        from repo_agent.evaluation.official_runner import audit_score
        if audit_score(source, report, output)['status'] == 'unscored':
            raise ValueError('Baseline official report lacks consistent test evidence')
    return {'report': report, 'sha256': digest(raw), 'source': 'operator_supplied_official_report',
            'environmentIdentity': 'requires_operator_verification', 'baseCommit': task['baseCommit']}


def run_agent(run_directory, *, baseline_report, max_steps, max_total_tokens, max_seconds,
              context_tokens=128_000, output_tokens=2048, tokenizer='auto', model=None,
              agent_tests=False, check_image=None, check_timeout=90, check_python=None,
              prompt_profile='standard', check_argv=None):
    directory, run, task = load_run(run_directory)
    if prompt_profile not in ('standard', 'concise'):
        raise ValueError('Unknown evaluation prompt profile')
    if (any(type(value) is not int for value in (max_steps, max_total_tokens, max_seconds))
            or not 1 <= max_steps <= 50 or max_total_tokens <= output_tokens or max_seconds < 1):
        raise ValueError('Invalid execution budgets')
    if type(agent_tests) is not bool or bool(check_image) != agent_tests or check_argv is not None and not agent_tests:
        raise ValueError('Developer checks require both agent_tests=True and an immutable check_image')
    if not (directory / 'prepared.json').is_file():
        raise ValueError('Workspace preparation did not complete')
    workspace = Path(run['workspace']).resolve(strict=True)
    if git(workspace, 'rev-parse', 'HEAD').stdout.decode().strip() != task['baseCommit']:
        raise ValueError('Workspace no longer matches the frozen base')
    if git(workspace, 'status', '--porcelain', '--untracked-files=all').stdout:
        raise ValueError('Workspace was already modified; prepare a fresh run')
    baseline = _baseline(directory, run, task, baseline_report)
    # A failed or interrupted attempt is retained and can never be silently rerun.
    with (directory / 'attempt.lock').open('x', encoding='utf-8') as stream:
        stream.write('An attempt has started. Prepare a new run to repeat.\n')
    save(directory, 'baseline.json', baseline)
    redactor = Redactor()
    started = time.monotonic()
    patch_ref, result, measured = None, None, None
    checks = None
    try:
        archive = archive_agent_source(directory / 'agent-source')
        if archive['status'] != 'captured':
            raise ValueError('Agent source snapshot did not complete')
        save(directory, 'runtime-packages.json', {'packages': sorted(
            [{'name': d.metadata['Name'], 'version': d.version}
             for d in importlib.metadata.distributions()], key=lambda item: item['name'].lower())})
        # No transport retry by default: every paid repeat must be an explicit trial.
        from repo_agent.evaluation.development_checks import CheckProvider, DevelopmentChecks, EvaluationTools
        checks = (DevelopmentChecks(directory, workspace, task, image=check_image,
                                     timeout=check_timeout, python=check_python,
                                     **({'argv': check_argv} if check_argv is not None else {})) if agent_tests else None)
        provider_class = CheckProvider if agent_tests else OpenAICompatibleProvider
        provider = model or provider_class(timeout=min(90, max_seconds), max_retries=0)
        budget = TokenBudget(provider.model, context_tokens=context_tokens, output_tokens=output_tokens,
                             reserve_tokens=4096, tokenizer=tokenizer)
        measured = _BudgetedModel(provider, budget, max_total_tokens, max_seconds, task=task, redactor=redactor)
        save(directory, 'execution-config.json', {'model': provider.model,
             'source': 'configured_provider' if model is None else 'injected_model',
             'profile': getattr(provider, 'profile', None), 'runtime': measured.get_run_metadata(),
             'maxSteps': max_steps, 'maxTotalEstimatedTokens': max_total_tokens,
             'promptProfile': prompt_profile,
             'maxTotalTokens': max_total_tokens,
             'legacyBudgetFieldNote': 'maxTotalEstimatedTokens is retained as an alias; this run uses versioned provider-usage settlement',
             'maxTotalAccountedTokens': max_total_tokens,
             'maxTotalTokenBudgetSemantics': 'provider_usage_or_conservative_preflight_estimate',
             'softMaxSeconds': max_seconds, 'contextTokens': context_tokens,
             'outputTokens': output_tokens, 'tokenizer': tokenizer,
             'allowWrites': True, 'agentTests': 'container_developer_checks' if agent_tests else 'disabled',
             'checkImage': check_image, 'checkTimeoutSeconds': check_timeout if agent_tests else None,
             'checkPython': str(check_python) if check_python else None,
             'completionRequiresCurrentCheck': agent_tests,
             'checkReminderAfterActions': CHECK_REMINDER_ACTIONS if agent_tests else None,
             'hiddenOfficialTestsVisible': False, 'workspaceIsSandbox': False})
        tools_class = EvaluationTools if agent_tests else TaskRepoTools
        tools = tools_class(workspace, **({'checks': checks} if checks else {}),
                           contract={'kind': 'fix_bug', 'allowTests': agent_tests,
                              'allowedPaths': ['.'], 'checkTargets': [],
                              'workspaceMode': 'direct', 'sourceKind': 'git'})
        tools.policy_check = lambda: git(workspace, 'rev-parse', 'HEAD').stdout.decode().strip() == task['baseCommit']
        agent = _RecordedAgent(measured, tools, trace_dir=directory / 'trace',
                               max_steps=max_steps, token_budget=budget)
        agent.redactor = redactor
        test_permissions = (
            'Developer checks are enabled in the verified container. Locate and run the relevant existing '
            'test file from the fixed target repository first with run_tests. SymPy and Django use their '
            'native test runners. If no relevant existing test is available or it leaves a concrete gap in '
            'the reported behavior, add a focused Python check with create_file under virtual .agent-checks/ '
            'and run it with run_tests. Base its assertions on the task and the fixed repository conventions. '
            'This virtual directory is outside the business checkout. Use read_file and replace_lines to revise scripts. '
            'Each check uses a fresh container with a snapshot of current business edits; source imports '
            'come from /testbed. Failed checks are feedback: correct the implementation or your check '
            'when justified, then rerun. Self-written assertions can be wrong; check repository '
            'conventions. Hidden official tests are unavailable. '
            'Use only the supplied task and files in this fixed checkout. '
            'Before writing checks, inspect the relevant existing test and implementation conventions. '
            'In every run_tests call, provide review with purpose (regression or control), '
            'criteria (exact task quote, scenario, expected result, expected_basis task/repository/assumption, '
            'and start_line/end_line in the test). For repository-based expectations, include evidence '
            'with repository file path, exact line range and quote from the fixed base. Mark unsupported '
            'format choices as assumptions and do not present them as established conventions. '
            'An unchanged test with the same hash can reuse its saved review; changing the script requires a fresh review. '
            'Explain coverage gaps honestly. Check that the scenario matches '
            'the requirement, including concurrency or nesting when relevant. '
            'Temporary checks run on base and candidate code in separate sequential containers; '
            'the pair shares one time budget. A failed base assertion is evidence of a version '
            'difference, not proof that the test expectation is correct. '
            'Rerun related existing tests after edits and report any missing regression coverage. '
            'Your self-tests remain needs_review until external acceptance. '
            'After implementing a change, prioritize running a focused check before further speculative edits. '
            'To finish completed/already_satisfied, a passing run_tests result must match the current '
            'business code and check scripts; changing either invalidates the result. '
            'If checks cannot be run or the change is still wrong, finish incomplete/blocked with '
            'the concrete reason and remaining work rather than claiming success. '
            if agent_tests else 'Agent tests are disabled; do not call run_tests. ')
        if prompt_profile == 'concise' and agent_tests:
            test_permissions = (
                'Developer checks are enabled. Start with run_tests on a relevant existing test file '
                'from the fixed target repository. Add a temporary check under .agent-checks/ only '
                'when the existing tests leave a concrete coverage gap or no relevant test exists. '
                'Checks run in fresh containers and provide feedback. '
                'Report actual results and remaining uncertainty. '
                'A passing current check is required to finish completed or already_satisfied. '
                'If unfinished or blocked, report the reason honestly. ')
        import hashlib
        save(directory, 'prompt-profile.json', {'profile': prompt_profile,
             'testPermissions': test_permissions,
             'sha256': hashlib.sha256(test_permissions.encode('utf-8')).hexdigest()})
        result = agent.run(task['problemStatement'] + '\n\nExecution permissions: edit this disposable '
                           'workspace only. ' + test_permissions +
                           'Do not modify existing tests or test/build configuration. '
                           'External official evaluation is separate; do not claim it has passed.')
        save(directory, 'agent-result.json', redactor.private_value(result))
        targets = checks.targets if checks else FrozenTestTargets(workspace, task['baseCommit'])
        patch_ref = targets.export(directory)
        patch = read_reference(directory, patch_ref).decode('utf-8')
        prediction = {'instance_id': task['taskId'], 'model_name_or_path': 'coding-agent', 'model_patch': patch}
        with (directory / 'predictions.jsonl').open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(prediction, ensure_ascii=False) + '\n')
    except BaseException as exc:
        save(directory, 'execution-error.json', {'errorType': type(exc).__name__,
             'message': redactor.private(str(exc))[:1000], 'officialGrade': 'not_run'})
        raise
    finally:
        verification = {'level': 'verification_incomplete', 'officialGrade': 'not_run'}
        if checks and patch_ref:
            try:
                verification = checks.verification_summary(patch_ref['sha256'])
            except (OSError, ValueError) as exc:
                verification['error'] = redactor.private(str(exc))[:1000]
        save(directory, 'attempt.json', {'schemaVersion': 1, 'runId': run['runId'],
             'taskId': task['taskId'], 'agentStatus': (result or {}).get('status', 'execution_error'),
             **delivery_verdict((result or {}).get('status', 'execution_error'),
                                (result or {}).get('model_reported_outcome'), verification),
             'patch': patch_ref, 'seconds': time.monotonic() - started,
             'reservedEstimatedTokens': measured.reserved if measured else 0,
             'tokenAccounting': measured.accounting() if measured else None,
             'modelCalls': measured.calls if measured else 0,
             'httpAttempts': measured.http_attempts if measured else 0,
             'usageReports': measured.usage_reports if measured else 0,
             'usage': measured.usage if measured else {}, 'cost': None,
             'usageCoverage': 'provider_usage_or_conservative_preflight_estimate',
             'officialGrade': 'not_run',
             'developerVerification': verification,
             'independentlyVerified': False})
    return directory
