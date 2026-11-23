"""SWE task preparation, complete patch export and official-report ingestion."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from repo_agent.evaluation.artifacts import digest, identifier, load_run, reference, save, read_reference
from repo_agent.infrastructure.redaction import Redactor
from repo_agent.backend.services.task_execution_tools import protected


DATASETS = {
    'lite': ('SWE-bench/SWE-bench_Lite', 'b0dde1093fe417d83b7184254edf8199c1f0dff5'),
    'verified': ('SWE-bench/SWE-bench_Verified', '78f471bf655a3137b2e8a75af1501690ec009ec3'),
}
GRADER_VERSION = '5.0.2'


def git(workspace, *arguments, env=None, check=True):
    result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.autocrlf=false',
                             '-c', 'core.hooksPath=' + os.devnull, '-C', str(workspace), *arguments],
                            capture_output=True, timeout=120, env=env)
    if check and result.returncode:
        raise ValueError(f'Git operation failed ({arguments[0]}); exit code {result.returncode}')
    return result


def select_instance(path, instance_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+', instance_id):
        raise ValueError('Invalid SWE instance_id')
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 256_000_000:
        raise ValueError('Export is too large; supply only the selected official records')
    raw = path.read_bytes()
    if path.suffix.lower() == '.jsonl':
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        data = json.loads(raw)
        rows = data if isinstance(data, list) else [data]
    selected = [row for row in rows if isinstance(row, dict) and row.get('instance_id') == instance_id]
    if len(selected) != 1:
        raise ValueError('Selected instance must occur exactly once in the export')
    row = selected[0]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', row.get('repo', '')):
        raise ValueError('Invalid upstream repository identity')
    if not re.fullmatch(r'[a-f0-9]{40}', row.get('base_commit', '')):
        raise ValueError('SWE base_commit must be a full commit SHA')
    if not isinstance(row.get('problem_statement'), str) or not row['problem_statement'].strip():
        raise ValueError('Missing problem statement')
    for key in ('patch', 'test_patch', 'version', 'FAIL_TO_PASS', 'PASS_TO_PASS'):
        if key not in row:
            raise ValueError(f'Official grading field is missing: {key}')
    if any(not isinstance(row[key], str) for key in ('patch', 'test_patch', 'version')):
        raise ValueError('Patch, test_patch and version must be strings')
    if not instance_id.startswith(row['repo'].replace('/', '__') + '-'):
        raise ValueError('Instance ID does not match the upstream repository')
    for key in ('FAIL_TO_PASS', 'PASS_TO_PASS'):
        tests = json.loads(row[key]) if isinstance(row[key], str) else row[key]
        if not isinstance(tests, list) or any(not isinstance(t, str) for t in tests):
            raise ValueError('Invalid frozen test inventory')
    return row, digest(raw)


def prepare(source_file, instance_id, source_repo, *, dataset='lite',
            runs_root='runs/evaluation/swe-bench', workspaces_root=None):
    """Copy a local source into a unique disposable checkout; no tests/model/network."""
    row, export_hash = select_instance(source_file, instance_id)
    source_repo = Path(source_repo).resolve(strict=True)
    top = Path(git(source_repo, 'rev-parse', '--show-toplevel').stdout.decode().strip()).resolve()
    if top != source_repo:
        raise ValueError('source_repo must be the repository root')
    git(source_repo, 'cat-file', '-e', row['base_commit'] + '^{commit}')
    workspaces_root = (Path(workspaces_root) if workspaces_root is not None else
                       Path('experiments') / row['repo'].replace('/', '__') / 'workspaces')
    runs_root, workspaces_root = Path(runs_root).resolve(), workspaces_root.resolve()
    if (runs_root.is_relative_to(source_repo) or workspaces_root.is_relative_to(source_repo)
            or runs_root.is_relative_to(workspaces_root) or workspaces_root.is_relative_to(runs_root)):
        raise ValueError('Sources, evidence and workspaces must be separate trees')
    run_id = identifier()
    directory = runs_root / run_id
    workspace = workspaces_root / f'{instance_id}_{run_id}_agent'
    directory.mkdir(parents=True, exist_ok=False)
    workspaces_root.mkdir(parents=True, exist_ok=True)
    redactor = Redactor()
    encoded = json.dumps(row, ensure_ascii=False)
    if redactor.private(encoded) != encoded:
        raise ValueError('Task input contains a known credential; raw input not saved')
    task = {'schemaVersion': 1, 'taskId': instance_id, 'dataset': DATASETS[dataset][0],
            'datasetRevision': DATASETS[dataset][1], 'split': 'test',
            'datasetProvenance': 'operator_supplied_local_export', 'exportSha256': export_hash,
            'repo': row['repo'], 'baseCommit': row['base_commit'],
            'problemStatement': row['problem_statement'], 'goldInputsVisibleToAgent': False,
            'testPatchSha256': digest(row['test_patch'].encode('utf-8')),
            'baselineVerification': 'required_before_agent_run', 'graderVersion': GRADER_VERSION}
    spec_ref = save(directory, 'task.json', task)
    # Hidden reference patch and test_patch stay outside the Agent's workspace.
    input_ref = save(directory, 'private-input/grading-input.json', [row])
    save(directory, 'run.json', {'schemaVersion': 1, 'runId': run_id, 'benchmark': 'swe-bench',
                               'taskSpec': spec_ref, 'gradingInput': input_ref,
                               'workspace': str(workspace), 'sourceRepo': str(source_repo),
                               'createdStatus': 'preparing', 'workspaceIsSandbox': False})
    try:
        git(workspaces_root, 'clone', '--no-hardlinks', '--no-checkout', '--', str(source_repo), str(workspace))
        git(workspace, 'checkout', '--detach', row['base_commit'])
        if git(workspace, 'status', '--porcelain').stdout:
            raise ValueError('Prepared workspace is not clean')
        save(directory, 'prepared.json', {'status': 'prepared', 'head': row['base_commit'],
                                         'baselineTests': 'not_run', 'agentExecuted': False})
    except BaseException as exc:
        save(directory, 'preparation-error.json', {'status': 'failed', 'errorType': type(exc).__name__})
        raise
    return directory


def export_patch(directory, workspace, base_commit):
    if git(workspace, 'rev-parse', 'HEAD').stdout.decode().strip() != base_commit:
        raise ValueError('Workspace HEAD changed from the fixed base')
    if git(workspace, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').stdout:
        raise ValueError('Ignored files appeared; complete patch export requires review')
    with tempfile.TemporaryDirectory(prefix='patch-index-', dir=directory) as temporary:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        git(workspace, 'read-tree', base_commit, env=env)
        git(workspace, 'add', '--all', '--', '.', env=env)
        changed = git(workspace, 'diff', '--cached', '--name-only', '-z', base_commit, env=env).stdout
        if any(protected(name, []) for name in changed.decode('utf-8').split('\0') if name):
            raise ValueError('Tests or frozen evaluation configuration changed; patch requires review')
        patch = git(workspace, 'diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv',
                    base_commit, '--', '.', env=env).stdout
    text = patch.decode('utf-8')
    if Redactor().private(text) != text:
        raise ValueError('Known credential in patch; refusing to export')
    target = directory / 'candidate.patch'
    with target.open('xb') as stream:
        stream.write(patch)
    return reference(directory, target)


def import_grade(run_directory, report_path, evaluated_patch, *, test_output=None, score_evidence=None):
    directory, run, task = load_run(run_directory)
    attempt = json.loads((directory / 'attempt.json').read_bytes())
    expected = read_reference(directory, attempt['patch'])
    actual = Path(evaluated_patch).read_bytes()
    comparison = 'exact_bytes' if actual == expected else 'windows_text_newline_translation'
    if actual != expected and actual != expected.replace(b'\n', b'\r\n'):
        raise ValueError('Official evaluated patch does not match the saved candidate bytes')
    raw = Path(report_path).read_bytes()
    report = json.loads(raw)
    if Redactor().private(raw.decode('utf-8')) != raw.decode('utf-8'):
        raise ValueError('Known credential in official report; refusing raw persistence')
    if not isinstance(report, dict) or task['taskId'] not in report:
        raise ValueError('Expected official per-instance report.json, not an aggregate summary')
    item = report[task['taskId']]
    if not isinstance(item, dict) or any(type(item.get(key)) is not bool for key in
                                        ('resolved', 'patch_exists', 'patch_successfully_applied')):
        raise ValueError('Official report flags are missing or not booleans')
    infra = item.get('infra_failure', False)
    if type(infra) is not bool:
        raise ValueError('Invalid infrastructure failure flag')
    if item['resolved'] and (not item['patch_exists'] or not item['patch_successfully_applied'] or infra):
        raise ValueError('Official resolved flag conflicts with application/infrastructure flags')
    if item['patch_exists'] != bool(expected.strip()):
        raise ValueError('Official patch_exists flag conflicts with the saved candidate')
    if score_evidence is not None:
        if (test_output is None or score_evidence['report_sha256'] != digest(raw)
                or score_evidence['output_sha256'] != digest(Path(test_output).read_bytes())):
            raise ValueError('Score evidence does not match the official report and log')
    elif test_output is not None:
        from repo_agent.evaluation.official_runner import audit_score
        row, = json.loads(read_reference(directory, run['gradingInput']))
        score_evidence = audit_score(row, report, test_output)
    grade_dir = directory / 'grades' / identifier('grade')
    grade_dir.mkdir(parents=True, exist_ok=False)
    with (grade_dir / 'official-report.json').open('xb') as stream:
        stream.write(raw)
    if test_output is not None:
        output = Redactor().private(Path(test_output).read_text(encoding='utf-8', errors='replace'))
        with (grade_dir / 'test-output.txt').open('x', encoding='utf-8') as stream:
            stream.write(output)
    unscored = (score_evidence or {}).get('status') == 'unscored'
    status = ('unscored' if unscored else 'unknown' if infra else 'pass' if item['resolved'] else
              'fail' if item['patch_successfully_applied'] else 'unknown')
    grade = {'schemaVersion': 1, 'runId': run['runId'], 'taskId': task['taskId'],
             'status': status, 'resolved': None if unscored else item['resolved'],
             'officialResolved': item['resolved'], 'scoreEvidence': score_evidence,
             'failureCategory': 'scorer_evidence' if unscored else 'infrastructure' if infra else
                                None if status == 'pass' else 'task' if status == 'fail' else 'patch_or_evaluator',
             'patchSha256': digest(expected), 'evaluatedPatchSha256': digest(actual),
             'patchComparison': comparison,
             'officialReport': reference(grade_dir, grade_dir / 'official-report.json'),
             'grader': 'swebench', 'graderVersion': task['graderVersion'],
             'graderVersionProvenance': 'declared_by_importer',
             'execution': {key: attempt.get(key) for key in
                           ('agentStatus', 'seconds', 'modelCalls', 'httpAttempts',
                            'reservedEstimatedTokens', 'tokenAccounting', 'usage',
                            'usageReports', 'cost')},
             'verification': 'external_report_with_matching_patch', 'independentlyVerified': False}
    grade['verificationSummary'] = {
        'level': 'external_pass' if status == 'pass' else 'external_fail' if status == 'fail' else 'verification_incomplete',
        'source': 'imported_external_report', 'environmentIndependentlyVerified': False}
    save(grade_dir, 'grade.json', grade)
    (grade_dir / 'summary.md').write_text(
        f'# SWE-bench result\n\nTask: {task["taskId"]}\n\nStatus: {status}\n\n'
        f'Official resolved: {item["resolved"]}\n\n'
        f'Agent status: {attempt.get("agentStatus", "not_recorded")}\n\n'
        f'Execution seconds: {attempt.get("seconds", "unknown")}\n\n'
        f'Provider token usage: {json.dumps(attempt.get("usage"), ensure_ascii=False)}\n\n'
        f'Budget accounting: {json.dumps(attempt.get("tokenAccounting"), ensure_ascii=False)}\n\n'
        'Cost: unknown (no price table configured).\n\n'
        'Imported external report; matching patch checked. No claim of independently verified environment identity.\n',
        encoding='utf-8')
    return grade_dir
