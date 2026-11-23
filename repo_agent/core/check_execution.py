"""Repository-specific check commands, result parsing and comparison."""
import ast
import re
import xml.etree.ElementTree as ET


TESTBED_PYTHON = '/opt/miniconda3/envs/testbed/bin/python'
REPORT_LIMIT = 2 * 1024 * 1024


class ConflictingTestReport(ValueError):
    """A report contains contradictory outcomes or disagrees with execution."""


def validate_check_argv(argv):
    if (not isinstance(argv, (list, tuple)) or not argv or
            any(not isinstance(arg, str) or not arg or '\0' in arg for arg in argv) or
            argv[0] in {'{target}', '{report}'} or argv.count('{target}') != 1):
        raise ValueError('Test argv must name an executable and contain one {target} argument')
    return list(argv)


def detect_repository_runner(root):
    if (root / 'tests/runtests.py').is_file() and (root / 'django/__init__.py').is_file():
        return 'django/django'
    if (root / 'bin/test').is_file() and (root / 'sympy/__init__.py').is_file():
        return 'sympy/sympy'
    return 'pytest'
DJANGO_CHECK_BOOTSTRAP = """import os
import runpy
import sys
import traceback
from pathlib import Path

script = sys.argv[1]
workspace = sys.argv[2] if len(sys.argv) > 2 else '/testbed'
sys.argv = [script]
sys.path[:0] = [os.path.dirname(script), workspace, os.path.join(workspace, 'tests')]
with Path(script).open(encoding='utf-8') as source:
    custom_settings = source.readline().strip() == '# agent-check: custom-django-settings'
if not custom_settings:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'test_sqlite')
    try:
        import django
        django.setup()
    except Exception:
        traceback.print_exc()
        print('Developer check: Django test initialization failed', file=sys.stderr)
        sys.exit(78)
runpy.run_path(script, run_name='__main__')
"""


def test_command(config):
    if not config['temporary'] and config.get('argv') is not None:
        values = {'{target}': config['target'], '{report}': config.get('report')}
        argv = [values.get(arg, arg) for arg in validate_check_argv(config['argv'])]
        if None in argv:
            raise ValueError('The configured test command requires a report destination')
        return argv
    python = [config.get('python', TESTBED_PYTHON), '-X',
              'pycache_prefix=' + config.get('cache', '/tmp/agent-check-cache')]
    target = config['target']
    if config['temporary']:
        script = config.get('scratch', '/tmp/agent-checks') + '/' + target.removeprefix('.agent-checks/')
        if config['repo'] == 'django/django':
            command = python + ['-c', DJANGO_CHECK_BOOTSTRAP, script]
            if config.get('workspace'):
                command.extend([config['workspace']])
            return command
        return python + [script]
    if config['repo'] == 'sympy/sympy':
        path, _, selector = target.partition('::')
        return python + ['bin/test', '-C', '--verbose', path] + (['-k', selector] if selector else [])
    if config['repo'] == 'django/django':
        path, *selectors = target.split('::')
        command = python + ['tests/runtests.py', '--settings=test_sqlite', '--parallel', '1', '--noinput']
        if target in {'.', 'tests'}:
            return command
        if not path.startswith('tests/') or selectors and not path.endswith('.py'):
            raise ValueError('Django checks require a test target under tests/')
        relative = path[len('tests/'):]
        parts = relative.removesuffix('.py').split('/') + selectors
        if not parts or any(not part.isidentifier() for part in parts):
            raise ValueError('Django test path or selector cannot be mapped to a test label')
        if parts[-1] == '__init__':
            parts.pop()
        if not parts:
            raise ValueError('Django test target must identify a test module')
        if len(selectors) == 2 and selectors[-1].startswith('test'):
            command.extend(['--verbosity', '2'])
        return command + ['.'.join(parts)]
    verbosity = '-v' if '::' in target else '-q'
    report_args = ['--junitxml=' + config['report']] if config.get('report') else []
    return python + ['-m', 'pytest', verbosity, *config.get('pytestOptions', []), *report_args, '--', target]


EXCEPTION_LINE = r'(?m)^(?:E[ \t]+)?(?:[\w.]+(?:Error|Exception|Warning)|AssertionError)(?::[^\n]*)?$'
TRACEBACK_HEADER = 'Traceback (most recent call last):'
TRACE_FRAME = re.compile(r'^\s+File "([^"]+)", line (\d+)(?:, in (.+))?$')


def validate_sympy_selector(source, target):
    _, _, selector = target.partition('::')
    if not selector:
        if '::' in target:
            raise ValueError('An empty SymPy selector is not a test')
        return
    if not selector.startswith('test_') or not selector.isidentifier():
        raise ValueError('SymPy requires one named test function after ::')
    try:
        nodes = ast.parse(source).body
    except (ValueError, SyntaxError) as exc:
        raise ValueError('Could not verify the SymPy selector from the repository test file') from exc
    matches = [node.name for node in nodes if isinstance(node, ast.FunctionDef)
               and node.name.startswith('test_') and selector in node.name]
    if matches != [selector]:
        raise ValueError('SymPy selector is missing or ambiguous in this test file')


def exception_chain(output):
    lines = output.splitlines()
    chain = []
    frames = None
    nested = False
    for index, line in enumerate(lines):
        if line == TRACEBACK_HEADER:
            if chain and not nested:
                chain = []
            frames = []
            nested = False
        elif line.startswith(('During handling of the above exception,',
                              'The above exception was the direct cause')):
            nested = True
        elif frames is not None:
            if match := TRACE_FRAME.match(line):
                path, number, function = match.groups()
                source = lines[index + 1].strip() if index + 1 < len(lines) else ''
                frames.append({'path': path, 'line': int(number), 'function': function, 'source': source})
            elif re.fullmatch(EXCEPTION_LINE, line):
                kind, _, _ = line.partition(':')
                chain.append({'exception': kind, 'detail': line[:700], 'frames': frames[-12:]})
                frames = None
    return chain


def primary_exception(output):
    chain = exception_chain(output)
    if chain:
        return chain[-1]['detail']
    details = re.findall(EXCEPTION_LINE, output)
    return details[-1][:700] if details else None


def failure_diagnostic(config, output, *, unittest_case=False):
    chain = exception_chain(output)
    primary = chain[-1] if chain else None
    location = primary['frames'][-1] if primary and primary['frames'] else None
    if config['temporary'] and chain:
        scratch = config.get('scratch', '/tmp/agent-checks').replace('\\', '/') + '/'
        script_frames = [frame for item in chain for frame in item['frames']
                         if frame['path'].replace('\\', '/').startswith(scratch)]
        if script_frames:
            location = script_frames[-1]
    detail = primary['detail'] if primary else primary_exception(output)
    diff = []
    if (primary and primary['exception'] == 'AssertionError'
            and (unittest_case or config.get('repo') == 'django/django' and not config['temporary'])):
        failure = output.rpartition(detail)[2]
        failure = re.split(r'(?m)^[-=]{5,}$', failure, maxsplit=1)[0]
        diff = [line for line in failure.splitlines() if line.startswith(('- ', '+ ', '? ', '  - ', '  + '))]
    has_diff = (any(line.lstrip().startswith('- ') for line in diff)
                and any(line.lstrip().startswith('+ ') for line in diff))
    return {'exception': primary['exception'] if primary else None,
            'location': location, 'exception_chain': chain[-4:],
            'traceback': output[-6000:] if chain else None,
            'traceback_excerpt_truncated': bool(chain and len(output) > 6000),
            'values': {'status': 'runner_diff', 'diff': '\n'.join(diff)[:1600]}
                      if has_diff else {'status': 'unavailable'}}


def temporary_failure_records(config, output):
    headings = re.split(r'(?m)^(FAIL|ERROR): (.+)\r?$', output)
    sections = [(name.strip(), kind, text) for kind, name, text in zip(headings[1::3], headings[2::3], headings[3::3])]
    totals = re.findall(r'(?m)^FAILED \(([^\n]+)\)\s*$', output)
    if not sections:
        blocks = re.split(r'(?m)(?=^' + re.escape(TRACEBACK_HEADER) + r')', output)
        chains = []
        for block in blocks[1:]:
            if chains and chains[-1].rstrip().endswith((
                    'During handling of the above exception, another exception occurred:',
                    'The above exception was the direct cause of the following exception:')):
                chains[-1] += block
            else:
                chains.append(block)
        sections = [(None, None, chain) for chain in chains]
    expected = (sum(int(count) for count in re.findall(r'(?:failures|errors)=(\d+)', totals[-1])) if totals
                else 1 if len(sections) == 1 and sections[0][0] is None else None)
    scratch = config.get('scratch', '/tmp/agent-checks').replace('\\', '/') + '/'
    records = []
    for name, outcome, body in sections[:12]:
        diagnosis = failure_diagnostic(config, body, unittest_case=name is not None)
        diagnosis.pop('traceback')
        location = diagnosis['location'] or {}
        path = location.get('path', '').replace('\\', '/')
        detail = diagnosis['exception_chain'][-1]['detail'] if diagnosis['exception_chain'] else primary_exception(body)
        if not detail:
            continue
        exception = diagnosis['exception'] or re.sub(r'^E[ \t]+', '', detail.partition(':')[0])
        diagnosis['exception'] = exception
        label = f'.agent-checks/{path[len(scratch):]}:{location["line"]}' if path.startswith(scratch) else name
        records.append({'test': name, 'runner_outcome': outcome, 'kind': failure_record_kind(diagnosis['exception']),
                        'diagnostic': diagnosis,
                        'evidence': f'{label} | {detail}' if label else None})
    complete = bool(records) and expected == len(sections) == len(records) and all(
        item['evidence'] and item['diagnostic']['location'] and len(item['evidence']) <= 700
        and not item['diagnostic']['traceback_excerpt_truncated']
        for item in records)
    return records, complete


def failure_record_kind(exception):
    if not exception:
        return 'unknown'
    if exception.endswith('Warning'):
        return 'behavior_warning'
    if exception.rsplit('.', 1)[-1] == 'AssertionError':
        return 'assertion_failure'
    return 'runtime_exception'


def repository_failure_records(config, output):
    if config['repo'] == 'django/django':
        heading = r'(?m)^(?:FAIL|ERROR): (.+)$'
    elif config['repo'] == 'sympy/sympy':
        heading = r'(?m)^_{2,}\s+([^\n]+\.py:[^\s]+)\s+_{2,}$'
    else:
        heading = r'(?m)^(?:FAILED|ERROR) (\S+(?: - [^\n]+)?)$'
    sections = re.split(heading, output)
    failures = []
    for name, body in zip(sections[1::2], sections[2::2]):
        detail = primary_exception(body)
        if detail or config['repo'] not in {'django/django', 'sympy/sympy'} and ' - ' in name:
            diagnostic = failure_diagnostic(config, body)
            diagnostic.pop('traceback')
            exception = diagnostic['exception'] or (detail.partition(':')[0] if detail else None)
            failures.append({'test': name, 'kind': failure_record_kind(exception), 'diagnostic': diagnostic,
                             'evidence': f'{name} | {detail}' if detail else name})
    return failures


def selected_repository_case(config, output):
    if config['temporary'] or '::' not in config.get('target', ''):
        return None
    target = config['target']
    path, *selectors = target.split('::')
    plain = re.sub(r'\x1b\[[0-9;]*m', '', output).replace('\\', '/').replace('\r\n', '\n')
    if config['repo'] == 'sympy/sympy':
        name = '::'.join(selectors)
        reports = re.findall(r'(?m)^(test_\w+)[ \t]+(ok|F|E|s|f|X|x)[ \t]*(?:\[[^\n]+\])?[ \t]*$', plain)
        listed_files = re.findall(r'(?m)^([^\n]+\.py)\[\d+\]', plain)
        outcome = reports[0][1] if listed_files == [path] and len(reports) == 1 and reports[0][0] == name else None
        return {'name': name, 'status': {'ok': 'passed', 'F': 'failed', 'E': 'error',
                                         's': 'not_passed', 'f': 'not_passed', 'X': 'not_passed',
                                         'x': 'not_passed'}.get(outcome, 'not_confirmed')}
    if config['repo'] == 'django/django' and len(selectors) == 2 and path.startswith('tests/'):
        cls, name = selectors
        module = path[len('tests/'):].removesuffix('.py').replace('/', '.')
        label = re.escape(module + '.' + cls)
        pattern = rf'(?m)^{re.escape(name)} \({label}(?:\.{re.escape(name)})?\)[^\n]*?\.\.\. (ok|FAIL|ERROR)\s*$'
        states = {'ok': 'passed', 'FAIL': 'failed', 'ERROR': 'error'}
    elif config['repo'] == 'pytest':
        pattern = rf'(?m)^{re.escape(target)} (PASSED|FAILED|ERROR)(?:[ \t]+\[[^\n]+\])?[ \t]*$'
        states = {'PASSED': 'passed', 'FAILED': 'failed', 'ERROR': 'error'}
    else:
        return None
    reports = re.findall(pattern, plain)
    return {'name': '::'.join(selectors), 'status': states[reports[0]] if len(reports) == 1 else 'not_confirmed'}


def check_result(config, exit_code, output, *, truncated=False):
    process_only = config['temporary'] and exit_code == 0
    if config['temporary']:
        passed = False
        counts = None
    else:
        if config['repo'] == 'sympy/sympy':
            match = re.search(r'tests finished:\s*([^\n]+)', output)
            summary = match.group(1) if match else ''
            names = {'passed': 'passed', 'failed': 'failed', 'exceptions': 'errors',
                     'errors': 'errors', 'error': 'errors', 'skipped': 'skipped'}
        elif config['repo'] == 'django/django':
            match = re.search(r'Ran\s+(\d+)\s+tests?\s+in\s+', output)
            ran = int(match.group(1)) if match else 0
            counts = {'passed': ran if exit_code == 0 else 0, 'failed': 0, 'errors': 0, 'skipped': 0}
            failed_summary = re.search(r'FAILED\s*\(([^\n]+)\)', output)
            if failed_summary:
                for name, number in re.findall(r'(failures|errors|skipped)=(\d+)', failed_summary.group(1)):
                    counts[{'failures': 'failed', 'errors': 'errors', 'skipped': 'skipped'}[name]] = int(number)
            success_summary = re.search(r'OK\s*\(([^\n]+)\)', output)
            if success_summary:
                skipped = re.search(r'skipped=(\d+)', success_summary.group(1))
                if skipped:
                    counts['skipped'] = int(skipped.group(1))
            counts['passed'] = max(0, ran - counts['failed'] - counts['errors'] - counts['skipped'])
            passed = exit_code == 0 and counts['passed'] > 0
        else:
            summaries = re.findall(r'(?m)^(?:=+[ \t]*)?(\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed)'
                                   r'[^\n]*? in \d+(?:\.\d+)?s)(?:[ \t]*=+)?[ \t]*\r?$', output)
            summary = summaries[-1] if summaries else ''
            names = {'passed': 'passed', 'failed': 'failed', 'errors': 'errors', 'error': 'errors', 'skipped': 'skipped'}
        if config['repo'] != 'django/django':
            counts = {'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0}
            for number, label in re.findall(r'(\d+) (passed|failed|exceptions|errors|error|skipped)\b', summary):
                if label in names:
                    counts[names[label]] = int(number)
            passed = exit_code == 0 and counts['passed'] > 0 and not counts['failed'] and not counts['errors']
    named_case = selected_repository_case(config, output)
    if named_case and config['repo'] == 'sympy/sympy':
        passed = passed and named_case['status'] == 'passed'
    detail = None if passed or process_only else primary_exception(output)
    failure_kind = None
    if process_only:
        failure_kind = 'evidence_unavailable'
    elif not passed:
        if exit_code in (124, 137, None):
            failure_kind = 'timeout' if exit_code == 124 else 'killed' if exit_code == 137 else 'not_executed'
        elif config['temporary']:
            failure_kind = ('assertion_failure' if re.search(r'^(?:[\w.]+\.)?AssertionError(?:[:\s]|$)', detail or '')
                            else 'execution_error')
        else:
            failure_kind = 'test_failure' if counts['failed'] and not counts['errors'] else 'execution_error'
    if passed or process_only:
        failure_records, records_complete = [], False
    elif config['temporary']:
        failure_records, records_complete = temporary_failure_records(config, output)
    else:
        failure_records = repository_failure_records(config, output)
        records_complete = (len(failure_records) == counts['failed'] + counts['errors']
                            and not any(item['diagnostic']['traceback_excerpt_truncated'] for item in failure_records))
    failures = [item['evidence'] for item in failure_records if item['evidence']]
    native = config.get('repo') in {'django/django', 'sympy/sympy'}
    ran = bool(counts and (counts['passed'] or counts['failed'] or native and counts['errors'] and failures))
    diagnostic = None if passed or process_only else failure_diagnostic(config, output)
    script_entered = config['temporary'] and diagnostic and diagnostic['location'] and failures
    import_site = (script_entered and diagnostic['location'].get('source', '').startswith(('import ', 'from ')))
    if truncated:
        passed, failure_stage = False, 'unknown'
    elif passed or process_only:
        failure_stage = None
    elif exit_code in (124, 126, 127, 137, None):
        failure_stage = 'infrastructure'
    elif ((exit_code == 78 and 'Developer check: Django test initialization failed' in output) or
          (not ran and (not script_entered or import_site) and re.search(r'^(?:E\s+)?(?:ModuleNotFoundError|ImportError|'
                                 r'django\.core\.exceptions\.ImproperlyConfigured|'
                                 r'django\.core\.exceptions\.AppRegistryNotReady):', detail or ''))):
        failure_stage = 'startup'
    elif failure_kind == 'assertion_failure':
        failure_stage = 'assertion'
    elif script_entered:
        failure_stage = 'test_execution'
    elif ran and exit_code == 1 and (counts['failed'] or counts['errors']):
        failure_stage = 'assertion' if counts['failed'] and not counts['errors'] else 'test_execution'
    elif not config['temporary'] and exit_code in (2, 3, 4) and not native:
        failure_stage = 'startup'
    else:
        failure_stage = 'unknown'
    unknown = process_only or failure_stage in {'infrastructure', 'startup', 'unknown', 'test_execution'}
    return {'passed': passed, 'exit_code': exit_code, 'counts': counts,
            'check_status': 'unknown' if unknown else 'passed' if passed else 'failed',
            **({'check_reason': 'TEST_EVIDENCE_UNAVAILABLE'} if process_only else {}),
            'failure_kind': failure_kind,
            'failure_stage': failure_stage,
            'failure_detail': detail,
            'diagnostic': diagnostic,
            **({'selected_case': named_case} if named_case else {}),
            'failure_evidence': [item[:700] for item in failures[:12]],
            'failure_records': failure_records[:12],
            'failure_evidence_complete': (
                not truncated and records_complete and bool(failures) and len(failures) <= 12
                and all(len(item) <= 700 for item in failures)),
            'timed_out': exit_code == 124, 'killed': exit_code == 137,
            'check_kind': 'model_authored_python' if config['temporary'] else 'existing_repository_tests',
            'officialGrade': False}


def execution_receipt(command, cwd, *, exit_code=None, timed_out=False, interrupted=False,
                      error=None, stage='tests', elapsed_seconds=None, environment=None,
                      source=None, logs=None):
    state = 'interrupted' if interrupted else 'timed_out' if timed_out else (
        'exited' if exit_code is not None else 'unknown' if stage == 'tests' and error else 'not_started')
    return {'schema_version': 1, 'argv': command, 'cwd': str(cwd), 'stage': stage,
            'status': state, 'exit_code': exit_code, 'timed_out': timed_out,
            'interrupted': interrupted, 'error': error, 'elapsed_seconds': elapsed_seconds,
            'environment': environment or {}, 'source': source or {},
            'logs': logs or {'status': 'not_available'}}


def junit_result(raw, target):
    if len(raw) > REPORT_LIMIT or b'<!DOCTYPE' in raw.upper() or b'\0' in raw:
        raise ValueError('Unsafe or oversized JUnit report')
    tree = ET.fromstring(raw)
    suites = [tree] if tree.tag == 'testsuite' else list(tree.findall('testsuite'))
    if not suites:
        raise ValueError('Missing JUnit suites')
    counts = dict(total=0, failed=0, errors=0, skipped=0, passed=0)
    for suite in suites:
        values = {name: int(suite.attrib[key]) for name, key in (
            ('total', 'tests'), ('failed', 'failures'), ('errors', 'errors'), ('skipped', 'skipped'))}
        values['passed'] = values['total'] - values['failed'] - values['errors'] - values['skipped']
        if min(values.values()) < 0:
            raise ConflictingTestReport('Inconsistent JUnit totals')
        for name, value in values.items():
            counts[name] += value
    path, *selectors = target.split('::')
    observed = {'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0}
    selected = []
    for case in tree.iter('testcase'):
        outcomes = [name for name, tag in (('errors', 'error'), ('failed', 'failure'),
                                         ('skipped', 'skipped')) if case.find(tag) is not None]
        if len(outcomes) > 1:
            raise ConflictingTestReport('Conflicting JUnit case outcomes')
        outcome = outcomes[0] if outcomes else 'passed'
        observed[outcome] += 1
        if selectors and case.get('name') == selectors[-1]:
            expected = '.'.join([path.removesuffix('.py').replace('/', '.'), *selectors[:-1]])
            classname = case.get('classname', '')
            if classname == expected or classname.endswith('.' + expected):
                selected.append(outcome)
    if any(observed.values()) and any(observed[k] != counts[k] for k in observed):
        raise ConflictingTestReport('JUnit cases disagree with suite totals')
    return counts, selected[0] if len(selected) == 1 else None


def interpret_execution(config, receipt, output, *, truncated=False, report=None):
    """Interpret the current run without turning process success into assertion evidence."""
    code = receipt['exit_code']
    custom = config.get('argv') is not None and not config['temporary']
    # An explicit command has no implicit pytest exit-code or output contract.
    text_invalid = False
    try:
        result = check_result(config, code if not custom else None, output if not custom else '',
                              truncated=truncated)
    except (ValueError, OverflowError):
        text_invalid = True
        result = check_result(config, code, '')
    result['execution'] = receipt
    result['exit_code'] = code
    result['timed_out'] = receipt['timed_out']
    evidence = {'source': 'execution', 'granularity': 'none',
                'report_status': 'missing' if config.get('report') else 'not_requested'}
    if text_invalid:
        evidence['text_status'] = 'invalid'
    result['test_evidence'] = evidence
    if receipt['status'] != 'exited' or receipt['stage'] != 'tests':
        stage = 'startup' if receipt['stage'] in {'setup', 'launch'} else 'infrastructure'
        result.update(passed=False, check_status='unknown', failure_stage=stage,
                      failure_detail=receipt['error'] or result['failure_detail'])
        if receipt['timed_out']:
            result['failure_kind'] = 'timeout'
        result['counts'] = None
        return result
    if result.get('failure_stage') in {'startup', 'infrastructure'} and not custom:
        result['counts'] = None
        evidence['report_status'] = 'not_checked' if config.get('report') else 'not_requested'
        return result
    if custom:
        result['failure_kind'] = None if code == 0 else 'execution_error'
    if report is not None and not config['temporary']:
        try:
            counts, selected = junit_result(report, config['target'])
            failure = counts['failed'] or counts['errors']
            if (code == 0 and failure or code != 0 and counts['passed'] and not failure
                    or counts['passed'] and not failure and result.get('failure_stage') == 'assertion'
                    or failure and result.get('passed')):
                raise ConflictingTestReport('JUnit results disagree with the execution')
        except ConflictingTestReport:
            evidence.update(source='junit', report_status='conflict')
            result.update(passed=False, check_status='unknown', failure_stage='unknown',
                          check_reason='TEST_REPORT_CONFLICT', selected_case=None, counts=None,
                          failure_records=[], failure_evidence=[], failure_evidence_complete=False)
            return result
        except (ValueError, KeyError, ET.ParseError):
            evidence['report_status'] = 'invalid'
        else:
            evidence.update(source='junit', granularity='suite', report_status='valid')
            passed = code == 0 and counts['passed'] > 0 and not failure
            skipped = counts['total'] > 0 and counts['skipped'] == counts['total']
            stage = None if passed else 'test_execution' if counts['errors'] else (
                'assertion' if counts['failed'] else 'unknown')
            reason = 'NO_TESTS_COLLECTED' if not counts['total'] else (
                'ALL_TESTS_SKIPPED' if skipped else None if passed else 'TEST_FAILURE')
            result.update(passed=passed, counts=counts, failure_stage=stage, check_reason=reason,
                          failure_kind=None if passed else result['failure_kind'],
                          failure_detail=None if passed else result['failure_detail'],
                          check_status='passed' if passed else 'skipped' if skipped else (
                              'failed' if stage == 'assertion' else 'unknown'))
            if '::' in config['target']:
                name = config['target'].rsplit('::', 1)[-1]
                result['selected_case'] = {'name': name, 'status':
                                           'passed' if selected == 'passed' else 'not_confirmed'}
                if selected:
                    evidence['granularity'] = 'case'
            return result
    if config['temporary'] and code == 0:
        return result
    if not custom and not truncated and (result['passed'] or result.get('failure_records')
                                        or any((result.get('counts') or {}).values())):
        selected = (result.get('selected_case') or {}).get('status') == 'passed'
        evidence.update(source='text', granularity='case' if selected or result.get('failure_records') else 'suite')
        if result['counts'] is not None:
            result['counts'] = {**result['counts'], 'total': sum(result['counts'].values())}
        if not result['passed'] and code == 0 and result['counts'] and (
                result['counts']['skipped'] == result['counts']['total']):
            result.update(check_status='skipped', check_reason='ALL_TESTS_SKIPPED')
    else:
        result.update(passed=False, check_status='unknown', failure_stage='unknown',
                      check_reason='TEST_EVIDENCE_UNAVAILABLE', counts=None,
                      failure_records=[], failure_evidence=[], failure_evidence_complete=False)
        if code == 0:
            result['failure_kind'] = 'evidence_unavailable'
        if truncated or custom:
            result.pop('selected_case', None)
    return result


def classify_result(baseline, candidate):
    evidence = {'candidate': 'test-output.txt', 'baseline': 'baseline/test-output.txt' if baseline else None}
    stages = {'startup', 'infrastructure'}
    phase = next((name for name, result in (('candidate', candidate), ('baseline', baseline))
                  if result and result.get('failure_stage') in stages), None)
    if phase:
        result = candidate if phase == 'candidate' else baseline
        code, reason = 'INFRA_ERROR', f'{phase} failed during {result["failure_stage"]} (exit {result["exit_code"]})'
        if result.get('failure_detail'):
            reason += ': ' + result['failure_detail']
    elif candidate['passed']:
        code, reason = 'PASS', 'Selected candidate check passed'
    elif candidate.get('exit_code') == 0 and candidate.get('check_reason') == 'TEST_EVIDENCE_UNAVAILABLE':
        code, reason = 'UNKNOWN', 'The command exited successfully. Test execution was not confirmed.'
    elif candidate.get('failure_stage') == 'candidate_syntax':
        code, reason = 'REGRESSION', 'Python syntax check failed on a changed candidate file'
    elif baseline and candidate.get('failure_stage') in {'assertion', 'test_execution'} and baseline['passed']:
        code, reason = 'REGRESSION', 'The selected check passed on base and failed on the candidate'
    elif baseline and all(item.get('failure_evidence_complete') for item in (baseline, candidate)):
        previous = set(baseline['failure_evidence'])
        current = set(candidate['failure_evidence'])
        if current <= previous:
            code, reason = 'BASELINE_FAILURE', 'All observed candidate failures also occurred on base'
        elif any(item.split(' | ', 1)[0] not in {failure.split(' | ', 1)[0] for failure in previous}
                 for item in current) and candidate['check_kind'] == 'existing_repository_tests':
            code, reason = 'REGRESSION', 'Candidate introduced a failure in a test that did not fail on base'
        else:
            code, reason = 'UNKNOWN', 'Candidate failure evidence differs from base; review both logs'
    else:
        code, reason = 'UNKNOWN', 'Candidate failure could not be attributed from the available evidence'
    return {'code': code, 'reason': reason, 'logs': evidence,
            'candidateFailures': candidate.get('failure_evidence', []),
            'baselineFailures': baseline.get('failure_evidence', []) if baseline else []}


def comparison_result(baseline, candidate, purpose):
    if purpose == 'control':
        return {'status': 'control', 'targetReproduced': None}
    if baseline is None:
        return {'status': 'not_run', 'targetReproduced': None}
    if candidate['passed'] and baseline.get('failure_evidence_complete'):
        kinds = {item['kind'] for item in baseline.get('failure_records', [])}
        if len(kinds) > 1:
            return {'status': 'baseline_mixed_failures_then_passed', 'targetReproduced': None,
                    'baselineFailureKinds': sorted(kinds)}
        if kinds == {'behavior_warning'} and baseline.get('failure_stage') == 'test_execution':
            return {'status': 'baseline_behavior_warning_then_passed', 'targetReproduced': None,
                    'baselineFailureKinds': sorted(kinds)}
    if baseline['check_status'] == 'unknown' or candidate['check_status'] == 'unknown':
        return {'status': 'incomplete', 'targetReproduced': None}
    if baseline['passed']:
        return {'status': 'base_already_passed', 'targetReproduced': False}
    if candidate['passed']:
        return {'status': 'assertion_failed_on_base_then_passed', 'targetReproduced': None}
    return {'status': 'still_failing', 'targetReproduced': None}
