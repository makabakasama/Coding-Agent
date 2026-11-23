"""Standalone container developer-check launcher. Never stages hidden grading R."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import time

if __package__:
    from repo_agent.core.check_execution import (REPORT_LIMIT, TESTBED_PYTHON, check_result, classify_result,
                                                comparison_result, execution_receipt, interpret_execution, test_command)
    from .container_lifecycle import (CONTAINER_LIMITS, MEMORY_BYTES, archive_check_output,
                                      container_slot, remove_container, require_idle)
else:
    from check_execution import (REPORT_LIMIT, TESTBED_PYTHON, check_result, classify_result,
                                 comparison_result, execution_receipt, interpret_execution, test_command)
    from container_lifecycle import (CONTAINER_LIMITS, MEMORY_BYTES, archive_check_output,
                                     container_slot, remove_container, require_idle)


def checked_bytes(root, ref):
    path = root / ref['path']
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Check material escapes its snapshot')
    raw = path.read_bytes()
    if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('Check material changed after snapshot')
    return raw


def archive_files(items):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, raw in items:
            parts = name.split('/')
            if name.startswith('/') or any(p in ('', '.', '..') for p in parts) or '\\' in name:
                raise ValueError('Invalid archive path')
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(raw), 0o644
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def execute_setup(container, command, directory):
    # First imports can emit Python syntax/deprecation warnings on stderr.
    # Those diagnostics must be preserved, not prefixed to machine-readable JSON.
    result = container.exec_run(command, workdir='/testbed', user='root', demux=True)
    stdout, stderr = result.output or (None, None)
    stdout = (stdout or b'').decode('utf-8', errors='replace')
    stderr = (stderr or b'').decode('utf-8', errors='replace')
    with (directory / 'setup-output.jsonl').open('ab') as stream:
        stream.write((json.dumps({'command': command, 'exitCode': result.exit_code,
                                  'stdout': stdout, 'stderr': stderr}) + '\n').encode('utf-8'))
    if result.exit_code:
        detail = (stderr or stdout).strip()[-2000:]
        raise ValueError('Developer check setup failed: ' + command[0] +
                         (': ' + detail if detail else ' (no diagnostic output)'))
    return stdout.strip()


def phase_receipt(config, directory, phase, command, **facts):
    source = {key: config.get(key) for key in ('baseCommit', 'tree')}
    source['patchSha256'] = config.get('patch', {}).get('sha256') if phase == 'candidate' else hashlib.sha256(b'').hexdigest()
    receipt = execution_receipt(command, '/testbed', source=source,
                                environment={'image': config.get('image'),
                                             'fingerprint': config.get('environmentSha256')}, **facts)
    pending = directory / 'execution.json.tmp'
    pending.write_bytes((json.dumps(receipt, indent=2) + '\n').encode())
    pending.replace(directory / 'execution.json')
    return receipt


def run_phase(client, config, directory, files, phase, deadline, check_id):
    """A fresh container per phase prevents checks from contaminating the next one."""
    container = None
    command = None
    stage = 'setup'
    started = time.monotonic()
    try:
        image = client.images.get(config['image'])
        container = client.containers.create(
            image.id, command=['sleep', 'infinity'], entrypoint=[], user='root',
            working_dir='/testbed', network_disabled=True, cap_drop=['ALL'],
            security_opt=['no-new-privileges:true'], **CONTAINER_LIMITS,
            environment={'PYTHONPATH': '/testbed', 'PYTHONNOUSERSITE': '1'},
            labels={'coding-agent.role': 'developer-check', 'coding-agent.check': check_id,
                    'coding-agent.phase': phase})
        (directory / 'container.json').write_bytes((json.dumps({
            'id': container.id, 'imageId': image.id, 'network': 'disabled',
            'hostBinds': [], 'memoryBytes': MEMORY_BYTES, 'cpus': 1, 'pids': 256,
            'globalContainerConcurrency': 1, 'phase': phase}) + '\n').encode('utf-8'))
        container.start()

        def execute(command):
            return execute_setup(container, command, directory)

        git = ['git', '-c', 'safe.directory=/testbed']
        head = execute(git + ['rev-parse', 'HEAD'])
        tree = execute(git + ['rev-parse', 'HEAD^{tree}'])
        if head != config['baseCommit'] or tree != config['tree'] or execute(git + ['status', '--porcelain']):
            raise ValueError('Developer-check image does not contain the fixed clean G/tree')
        if not container.put_archive('/tmp', archive_files(files)):
            raise ValueError('Could not stage developer-check snapshots')
        if phase == 'candidate' and config['patch']['bytes']:
            execute(git + ['apply', '--binary', '/tmp/agent.patch'])
        uses_testbed_python = config.get('argv') is None or config.get('temporary')
        changed_files = execute(git + ['diff', '--name-only', '-z']).split('\0')
        python_files = [name for name in changed_files if name.endswith('.py')]
        if uses_testbed_python and phase == 'candidate' and python_files:
            syntax_command = ['/opt/miniconda3/envs/testbed/bin/python', '-X',
                              'pycache_prefix=/tmp/agent-syntax-cache', '-m', 'py_compile', *python_files]
            syntax_result = container.exec_run(syntax_command, workdir='/testbed', user='root', demux=True)
            syntax_stdout, syntax_stderr = syntax_result.output or (b'', b'')
            syntax_output = ((syntax_stdout or b'') + (syntax_stderr or b'')).decode('utf-8', errors='replace')
            (directory / 'syntax-check.json').write_text(json.dumps({
                'command': syntax_command, 'exitCode': syntax_result.exit_code,
                'output': syntax_output[:12000], 'interpreter': 'testbed',
                'files': python_files}, ensure_ascii=False, indent=2), encoding='utf-8')
            if syntax_result.exit_code:
                (directory / 'test-output.txt').write_text(syntax_output, encoding='utf-8')
                receipt = phase_receipt(config, directory, phase, syntax_command, stage='syntax',
                                        exit_code=syntax_result.exit_code, elapsed_seconds=time.monotonic()-started,
                                        logs={'status': 'excerpt', 'combined': {'path': 'test-output.txt'}})
                return {'passed': False, 'exit_code': syntax_result.exit_code, 'counts': None,
                        'check_status': 'failed', 'failure_kind': 'syntax_error',
                        'failure_stage': 'candidate_syntax', 'failure_evidence': [syntax_output[-700:]],
                        'timed_out': False, 'killed': False, 'check_kind': 'syntax_check',
                        'officialGrade': False, 'phase': phase, 'containerId': container.id,
                        'execution': receipt, 'test_evidence': {'source': 'execution', 'granularity': 'none'},
                        'syntax_check': {'status': 'failed', 'interpreter': 'testbed',
                                         'output': syntax_output[:12000]}}
        # Public images can carry future commits. Remove that history before checks.
        execute(['rm', '-rf', '/testbed/.git'])
        runtime = {}
        if uses_testbed_python:
            origin_code = ('import json,sys; data={"python":sys.version,"executable":sys.executable}; '
                           + ('import sympy; data["source"]=sympy.__file__; ' if config['repo'] == 'sympy/sympy' else '')
                           + 'print(json.dumps(data))')
            origin = execute([TESTBED_PYTHON, '-X', 'pycache_prefix=/tmp/agent-origin-cache', '-c', origin_code])
            runtime = json.loads(origin)
            if config['repo'] == 'sympy/sympy' and runtime.get('source') != '/testbed/sympy/__init__.py':
                raise ValueError('Developer check imported a different Sympy source')
        (directory / 'source.json').write_bytes((json.dumps({
            'baseCommit': head, 'tree': tree, 'phase': phase,
            'patchSha256': config['patch']['sha256'] if phase == 'candidate' else hashlib.sha256(b'').hexdigest(),
            'gitHistoryAvailableToChecks': False, 'runtime': runtime}) + '\n').encode('utf-8'))
        config = dict(config)
        if not config['temporary']:
            argv = config.get('argv')
            if argv is None or '{report}' in argv:
                config['report'] = '/tmp/agent-check-report.xml'
        command = test_command(config)
        report_path = config.get('report')
        if report_path and report_path not in command and '--junitxml=' + report_path not in command:
            config.pop('report')
        (directory / 'command.json').write_bytes((json.dumps(command) + '\n').encode('utf-8'))
        adapter = ('django_test_settings' if config['temporary'] and config['repo'] == 'django/django'
                   else 'python_script' if config['temporary']
                   else 'command' if config.get('argv') is not None
                   else {'django/django': 'django_unittest', 'sympy/sympy': 'sympy_native'}.get(config['repo'], 'pytest'))
        (directory / 'test-environment.json').write_text(json.dumps({
            'adapter': adapter, 'interpreter': TESTBED_PYTHON if uses_testbed_python else None, 'workdir': '/testbed',
            'settingsModule': 'test_sqlite' if config['repo'] == 'django/django' else None,
        }), encoding='utf-8')
        if not config['temporary'] and config.get('argv') is None:
            if config['repo'] == 'django/django':
                preflight = ('import pathlib; assert pathlib.Path("tests/runtests.py").is_file(), '
                             '"Django tests/runtests.py is missing"; import django; '
                             'print("Django test runner available:", django.__file__)')
            elif config['repo'] == 'sympy/sympy':
                preflight = ('import pathlib; assert pathlib.Path("bin/test").is_file(), '
                             '"SymPy bin/test is missing"; print("SymPy test runner available")')
            else:
                preflight = ('import importlib.util; assert importlib.util.find_spec("pytest") is not None, '
                             '"pytest is unavailable in the testbed interpreter"; '
                             'print("pytest test runner available")')
            execute(['/opt/miniconda3/envs/testbed/bin/python', '-c', preflight])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = 'Overall developer-check time budget exhausted before execution'
            (directory / 'test-output.txt').write_bytes(error.encode('utf-8'))
            receipt = phase_receipt(config, directory, phase, command, stage='setup',
                                    timed_out=True, error=error, elapsed_seconds=time.monotonic()-started)
            record = interpret_execution(config, receipt, '')
            record['output_archive'] = {'status': 'not_created'}
            record['full_output_location'] = None
        else:
            # Program-owned shell syntax; the selected path is a separate argv value.
            stage = 'tests'
            test_start = time.monotonic()
            result = container.exec_run(
                ['/bin/bash', '-c', 'timeout --signal=TERM --kill-after=5 "$@" > /tmp/agent-check-output.txt 2>&1',
                 'developer-check', str(remaining), *command], workdir='/testbed', user='root')
            elapsed = time.monotonic() - test_start
            facts = {'exit_code': result.exit_code, 'timed_out': result.exit_code == 124,
                     'elapsed_seconds': elapsed}
            phase_receipt(config, directory, phase, command, **facts)
            archive = archive_check_output(container, check_id + '-' + phase)
            log = container.exec_run(['head', '-c', '1000001', '/tmp/agent-check-output.txt'])
            raw = log.output if log.exit_code == 0 else b''
            output = raw[:1_000_000].decode('utf-8', errors='replace')
            (directory / 'test-output.txt').write_bytes(output.encode('utf-8'))
            receipt = phase_receipt(
                config, directory, phase, command, **facts,
                logs={'status': 'complete' if archive['status'] == 'archived' and not facts['timed_out'] else 'partial',
                      'combined': {'path': 'test-output.txt', 'truncated': len(raw) > 1_000_000,
                                   'read_error': log.exit_code != 0}, 'archive': archive})
            report = None
            if config.get('report'):
                machine = container.exec_run(['head', '-c', str(REPORT_LIMIT + 1), config['report']])
                if machine.exit_code == 0:
                    report = machine.output
                    if len(report) <= REPORT_LIMIT:
                        (directory / 'report.xml').write_bytes(report)
            record = interpret_execution(config, receipt, output, truncated=len(raw) > 1_000_000 or log.exit_code != 0,
                                         report=report)
            record.update(output_truncated=len(raw) > 1_000_000, output_archive=archive,
                          full_output_location=archive['path'] if archive['status'] == 'archived' else None)
        record.update(phase=phase, containerId=container.id, adapter=adapter,
                      commandEvidence='command.json', environmentEvidence='test-environment.json')
        return record
    except Exception as exc:
        if not (directory / 'execution.json').exists():
            phase_receipt(config, directory, phase, command, stage=stage,
                          error=f'{type(exc).__name__}: {exc}', elapsed_seconds=time.monotonic()-started,
                          logs={'status': 'partial', 'setup': {'path': 'setup-output.jsonl'}
                                if (directory / 'setup-output.jsonl').exists() else None})
        raise
    finally:
        if container is not None:
            remove_container(container, directory, 'container')


def _main(request_file):
    import docker
    directory = Path(request_file).resolve(strict=True).parent
    config = json.loads((directory / 'request.json').read_bytes())
    patch = checked_bytes(directory, config['patch'])
    files = [('agent.patch', patch)]
    for ref in config['checks']:
        files.append(('agent-checks/' + ref['path'], checked_bytes(directory / 'checks', ref)))
    client = docker.from_env(timeout=config['timeout'] + 60)
    deadline = time.monotonic() + config['timeout']
    try:
        require_idle(client)
        baseline = None
        if config.get('compareBaseline', False):
            baseline_dir = directory / 'baseline'
            baseline_dir.mkdir(exist_ok=False)
            # Do not stage the candidate patch in the base container.
            baseline_files = [(name, raw) for name, raw in files if name != 'agent.patch']
            baseline = run_phase(client, config, baseline_dir, baseline_files, 'baseline', deadline, directory.name)
            (baseline_dir / 'result.json').write_bytes((json.dumps(baseline, indent=2) + '\n').encode('utf-8'))
        if time.monotonic() >= deadline:
            receipt = phase_receipt(config, directory, 'candidate', None, stage='setup', timed_out=True,
                                    error='Overall developer-check time budget exhausted before execution')
            record = interpret_execution(config, receipt, '')
            (directory / 'test-output.txt').write_bytes(b'Overall developer-check time budget exhausted')
        else:
            record = run_phase(client, config, directory, files, 'candidate', deadline, directory.name)
        if (baseline is None and not config.get('temporary') and time.monotonic() < deadline
                and record.get('failure_stage') in {'assertion', 'test_execution'}):
            baseline_dir = directory / 'baseline'
            baseline_dir.mkdir(exist_ok=False)
            baseline_files = [(name, raw) for name, raw in files if name != 'agent.patch']
            baseline = run_phase(client, config, baseline_dir, baseline_files, 'baseline', deadline, directory.name)
            (baseline_dir / 'result.json').write_bytes((json.dumps(baseline, indent=2) + '\n').encode('utf-8'))
        record.update(patchSha256=config['patch']['sha256'], baseline=baseline,
                      comparison=comparison_result(baseline, record, config.get('purpose', 'regression')),
                      classification=classify_result(baseline, record))
        (directory / 'result.json').write_bytes((json.dumps(record, indent=2) + '\n').encode('utf-8'))
    finally:
        client.close()


def main(request_file):
    with container_slot():
        return _main(request_file)


if __name__ == '__main__':
    main(sys.argv[1])
