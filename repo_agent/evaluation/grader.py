"""Explicit invocation of the pinned external SWE evaluator, one task at a time."""
import json
from pathlib import Path
import subprocess
import sys

from repo_agent.evaluation.artifacts import identifier, load_run, read_reference, save, reference
from repo_agent.evaluation.swebench import GRADER_VERSION, import_grade, git
from repo_agent.infrastructure.redaction import Redactor


def grading_command(run_directory, *, python=None, timeout=600, grading_id=None,
                    launcher=None, grading_input=None, predictions=None, git_env=None):
    directory, run, task = load_run(run_directory)
    attempt = json.loads((directory / 'attempt.json').read_bytes())
    patch = read_reference(directory, attempt['patch']).decode('utf-8')
    prediction = [json.loads(line) for line in (directory / 'predictions.jsonl').read_text(encoding='utf-8').splitlines()]
    if prediction != [{'instance_id': task['taskId'], 'model_name_or_path': 'coding-agent', 'model_patch': patch}]:
        raise ValueError('Official prediction differs from the recorded task/patch')
    if type(timeout) is not int or timeout < 1:
        raise ValueError('Grading timeout must be a positive integer')
    tree = git(run['sourceRepo'], 'rev-parse', task['baseCommit'] + '^{tree}', env=git_env).stdout.decode().strip()
    grading_id = grading_id or identifier('verify')
    command = [str(python or sys.executable),
               str(launcher or Path(__file__).with_name('official_runner.py').resolve()),
               '--expected-base', task['baseCommit'], '--expected-tree', tree,
               '--dataset_name', str(grading_input or (directory / run['gradingInput']['path']).resolve()),
               '--predictions_path', str(predictions or directory / 'predictions.jsonl'),
               '--instance_ids', task['taskId'], '--max_workers', '1',
               '--run_id', grading_id, '--timeout', str(timeout)]
    return command


def execute_grader(run_directory, *, python=None, timeout=600, runtime=None, git_env=None):
    """Called only by explicit `grade --execute`; starts Docker/tests, never a model."""
    directory, run, task = load_run(run_directory)
    python = runtime.python if runtime else str(python or sys.executable)
    if runtime:
        environment = runtime.manifest['environment']
    else:
        version = subprocess.run([python, '-c',
                             'import importlib.metadata as m,json,sys,platform; '
                             'print(json.dumps({"swebench":m.version("swebench"),'
                             '"python":sys.version,"executable":sys.executable,"platform":platform.platform(),'
                             '"packages":[{"name":d.metadata["Name"],"version":d.version} for d in m.distributions()]}))'],
                             capture_output=True, text=True, timeout=30)
        environment = json.loads(version.stdout) if version.returncode == 0 else {}
    if environment.get('swebench') != GRADER_VERSION:
        raise ValueError(f'The grading interpreter must have swebench=={GRADER_VERSION}')
    grading_id = identifier('verify')
    # Validate candidate and source before allocating a grading attempt.
    grading_command(directory, python=python, timeout=timeout, grading_id=grading_id, git_env=git_env)
    destination = directory / 'official' / grading_id
    destination.mkdir(parents=True, exist_ok=False)
    launcher = destination / 'official-runner.py'
    lifecycle = destination / 'container_lifecycle.py'
    provider = destination / 'image_provider.py'
    if runtime:
        runtime.stage(destination)
    else:
        launcher.write_bytes(Path(__file__).with_name('official_runner.py').read_bytes())
        lifecycle.write_bytes(Path(__file__).with_name('container_lifecycle.py').read_bytes())
        provider.write_bytes(Path(__file__).with_name('image_provider.py').read_bytes())
    grading_input = destination / 'frozen-grading-input.json'
    grading_input.write_bytes(read_reference(directory, run['gradingInput']))
    predictions = destination / 'predictions.jsonl'
    predictions.write_bytes((directory / 'predictions.jsonl').read_bytes())
    command = grading_command(directory, python=python, timeout=timeout, grading_id=grading_id,
                              launcher=launcher, grading_input=grading_input, predictions=predictions, git_env=git_env)
    if runtime:
        command = runtime.command(command)
    save(destination, 'environment.json', environment)
    save(destination, 'command.json', {'command': command, 'graderVersion': GRADER_VERSION,
                                     'modelCalls': False, 'concurrency': 1,
                                     'launcher': reference(destination, launcher),
                                     'containerLifecycle': reference(destination, lifecycle),
                                     'imageProvider': reference(destination, provider),
                                     'gradingInput': reference(destination, grading_input),
                                     'predictions': reference(destination, predictions),
                                     **({'scoringRuntimeSha256': runtime.identity} if runtime else {}),
                                     'adaptations': ['verify/restore image source',
                                                     'reuse content-keyed verified image',
                                                     'preserve Linux artifact LF']})
    redactor = Redactor()
    code, timed_out, drift = None, False, None
    try:
        if runtime:
            completed, drift = runtime.run(command, destination, timeout + 300)
        else:
            completed = subprocess.run(command, cwd=destination, capture_output=True, timeout=timeout + 300)
        code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout, stderr = exc.stdout or b'', exc.stderr or b''
    except BaseException as exc:
        save(destination, 'execution.json', {'status': 'interrupted_or_failed',
             'errorType': type(exc).__name__, 'containerCleanup': 'not_confirmed'})
        raise
    for name, raw in (('stdout.log', stdout), ('stderr.log', stderr)):
        (destination / name).write_bytes(redactor.output(raw))
    save(destination, 'execution.json', {'status': 'timed_out' if timed_out else 'exited',
         'exitCode': code,
         'containerCleanup': 'not_confirmed' if timed_out or code else 'managed_by_official_harness',
         'probeContainers': 'remove_after_recording; inspect cleanup records on interruption'})
    # Pinned 5.0.2 wheel uses this path (some newer docs say logs/evaluation).
    logs = destination / 'logs/run_evaluation' / grading_id / 'coding-agent' / task['taskId']
    evidence_file = destination / 'score-evidence.json'
    evidence = json.loads(evidence_file.read_bytes()) if evidence_file.is_file() else None
    verified = not runtime or (evidence and evidence.get('status') == 'consistent'
                               and (logs / 'test_output.txt').is_file())
    if (code == 0 and not timed_out and not drift and verified
            and (logs / 'report.json').is_file() and (logs / 'patch.diff').is_file()):
        grade = import_grade(directory, logs / 'report.json', logs / 'patch.diff',
                             test_output=logs / 'test_output.txt' if (logs / 'test_output.txt').is_file() else None,
                             score_evidence=evidence)
        save(destination, 'imported-grade.json', {'gradeDirectory': str(grade),
             'actualGraderVersion': GRADER_VERSION, 'versionCheck': 'passed',
             **({'scoringRuntimeSha256': runtime.identity} if runtime else {})})
    else:
        save(destination, 'ungraded.json', {'status': 'unknown', 'resolved': None,
             'reason': 'scoring_file_drift' if drift else 'timeout' if timed_out else
                       'evaluator_failed' if code else 'unverified_score_evidence' if not verified else 'no_per_instance_report',
             **({'detail': drift} if drift else {}),
             'note': 'An empty patch or evaluator/environment failure must not be counted as success.'})
    return destination
