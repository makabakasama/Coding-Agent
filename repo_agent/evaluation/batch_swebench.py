"""SWE-bench adapter for the production batch. Agent workers run frozen code."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from repo_agent.core.config import CONFIG_KEYS, read_dotenv
from repo_agent.evaluation.artifacts import digest, load_run, read_reference, reference, save
from repo_agent.evaluation.batch import admission, bound_input, classify_probe, compatible, load, serial_slot
from repo_agent.evaluation.container_lifecycle import require_idle
from repo_agent.evaluation.grader import execute_grader
from repo_agent.evaluation.official_runner import audit_score
from repo_agent.evaluation.scoring_runtime import ScoringError
from repo_agent.evaluation.runner import _baseline
from repo_agent.evaluation.swebench import git, prepare
from repo_agent.infrastructure.redaction import Redactor


PROVIDER_PROBE = '''
import hashlib,json
from repo_agent.core.provider import OpenAICompatibleProvider
p=OpenAICompatibleProvider(max_retries=0)
assert p.model and p.base_url and p.api_key
print(json.dumps({'model':p.model,'profile':p.profile,
                  'endpointSha256':hashlib.sha256(p.base_url.encode()).hexdigest(),
                  'runtime':p.get_run_metadata()}))
'''

DEVELOPER_PROBE = '''
import sys
from pathlib import Path
from repo_agent.evaluation.artifacts import load_run,save
from repo_agent.evaluation.development_checks import DevelopmentChecks
root, baseline, image, target, timeout = sys.argv[1:]
_, run, task = load_run(baseline)
root = Path(root)
check = DevelopmentChecks(root, Path(run['workspace']), task, image=image, timeout=int(timeout))
save(root,'result.json',check.run(target))
'''

FROZEN_WORKER = '''
import runpy,sys
from pathlib import Path
source, lock, *arguments = sys.argv[1:]
guard = runpy.run_path(source)['serial_slot']
with guard(Path(lock)):
    sys.argv = ['repo_agent.evaluation', *arguments]
    runpy.run_module('repo_agent.evaluation',run_name='__main__',alter_sys=True)
'''


def file_ref(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': digest(path.read_bytes())}


def verify_file(ref):
    path = Path(ref['path']).resolve(strict=True)
    if digest(path.read_bytes()) != ref['sha256']:
        raise ValueError(f'Evidence digest differs: {path.name}')
    return path


def verify_snapshot(arm):
    root = Path(arm['snapshot'])
    env = scoped_git_env([root])
    head = git(root, 'rev-parse', 'HEAD', env=env).stdout.decode().strip()
    if head != arm['commit'] or git(root, 'status', '--porcelain', env=env).stdout:
        raise ValueError('Frozen arm checkout differs from registration')
    verify_file(arm['bundle'])


def scoped_git_env(paths, base=None):
    env = dict(os.environ if base is None else base)
    exact = list(dict.fromkeys(Path(path).resolve().as_posix() for path in paths))
    env['GIT_CONFIG_COUNT'] = str(len(exact) + 1)
    for index, path in enumerate(exact):
        env[f'GIT_CONFIG_KEY_{index}'] = 'safe.directory'
        env[f'GIT_CONFIG_VALUE_{index}'] = path
    env[f'GIT_CONFIG_KEY_{len(exact)}'] = 'core.longpaths'
    env[f'GIT_CONFIG_VALUE_{len(exact)}'] = 'true'
    return env


def provider_environment(project, arms):
    keys = CONFIG_KEYS - {'DATABASE_URL', 'REDIS_URL'}
    env = {key: value for key, value in os.environ.items() if key not in keys | {'PYTHONPATH'}}
    env.update({key: value for key, value in read_dotenv(project / '.env').items() if key in keys})
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    settings = {}
    for name, arm in arms.items():
        result = subprocess.run([sys.executable, '-B', '-c', PROVIDER_PROBE], cwd=arm['snapshot'],
                                env=env, capture_output=True, check=True, timeout=30)
        settings[name] = json.loads(result.stdout)
    if settings['A'] != settings['B']:
        raise ValueError('Provider settings differ between frozen arms')
    return env, settings['A']


def resources(protocol, *, verify_engine=False):
    import docker
    for drive, minimum in protocol['minimumFreeGiB'].items():
        if shutil.disk_usage(f'{drive}:/').free < minimum * 1024**3:
            raise RuntimeError(f'{drive} drive is below the frozen disk floor')
    with closing(docker.from_env(timeout=60)) as client:
        require_idle(client)
        if verify_engine and not 0 < client.info()['MemTotal'] <= 8*1024**3:
            raise RuntimeError('Docker memory ceiling differs from the validated host')


def prove_environment(task, protocol):
    spec = task['conditions']['A']
    settings = spec['environment']
    baseline, saved, prepared = load_run(task['baselineDirectory'])
    image = bound_input(task['imageProof']['path'], task['imageProof']['sha256'])
    if (prepared['taskId'] != task['task'] or prepared['baseCommit'] != settings['baseCommit']
            or image['imageId'] != settings['image'] or image['expectedTree'] != spec['tree']
            or image['sourceVerified'] is not True or saved['sourceRepo'] != task['source']):
        raise ValueError('Fixed source or image proof differs from the task registration')
    sample, = [item for item in load(verify_file(protocol['sampleManifest']))['samples']
               if item['instance_id'] == task['task']]
    if (prepared['datasetRevision'] != protocol['datasetRevision']
            or settings['baseCommit'] != sample['base_commit']
            or digest(prepared['problemStatement'].encode()) != sample['publicPromptSha256']
            or spec['taskRevision']['publicPromptSha256'] != sample['publicPromptSha256']):
        raise ValueError('Frozen dataset or public problem statement differs from registration')
    frozen_row, = json.loads(read_reference(baseline, saved['gradingInput']))
    pinned_row = bound_input(task['dataset']['path'], task['dataset']['sha256'])
    if not isinstance(pinned_row, dict) or pinned_row != frozen_row:
        raise ValueError('Registered grading row differs from the accepted preparation')
    root = Path(task['source'])
    workspace = Path(saved['workspace'])
    env = scoped_git_env([root, workspace])
    for path in (root, workspace):
        tree = git(path, 'rev-parse', 'HEAD^{tree}', env=env).stdout.decode().strip()
        head = git(path, 'rev-parse', 'HEAD', env=env).stdout.decode().strip()
        if tree != spec['tree'] or head != settings['baseCommit'] or git(
                path, 'status', '--porcelain', env=env).stdout:
            raise ValueError('Local checkout differs from the fixed clean source')
    import docker
    with closing(docker.from_env(timeout=60)) as client:
        if client.images.get(settings['image']).id != settings['image']:
            raise ValueError('Effective image identity is unavailable')
    return baseline, saved, prepared, frozen_row


def prove_scoring(task, known):
    baseline, saved, prepared, row = known
    report = verify_file(task['baselineReport'])
    output = verify_file(task['baselineLog'])
    proof = audit_score(row, load(report), output)
    if proof['status'] != 'consistent':
        return {'state': 'unscored', 'reason': proof.get('reason', 'missing_evaluator_evidence'), 'audit': proof}
    _baseline(baseline, saved, prepared, report)
    return {'state': 'verified', 'audit': proof, 'report': task['baselineReport']}


def prove_log(result):
    archive = result.get('output_archive') or (result.get('execution') or {}).get('logs', {}).get('archive') or {}
    if archive.get('status') == 'archived':
        path = Path(archive['path'])
        if path.stat().st_size != archive['bytes'] or not 0 < archive['bytes'] <= 32 * 1024**2:
            raise ValueError('Developer output archive has incorrect size')
        verify_file(archive)


def run_probe(task, arm, protocol, directory, workspace):
    snapshot = protocol['arms'][arm]['snapshot']
    spec = task['conditions'][arm]
    old = task.get('priorProbes', {}).get(arm)
    if old:
        result_path = verify_file(old)
        request = bound_input(old['request']['path'], old['request']['sha256'])
        if (request['image'], request['baseCommit'], request['tree'], request['target']) != (
                spec['environment']['image'], spec['environment']['baseCommit'], spec['tree'], task['probeTarget']):
            raise ValueError('Archived developer probe is bound to different inputs')
    else:
        resources(protocol)
        cmd = [sys.executable, '-B', '-c', DEVELOPER_PROBE, str(directory), task['baselineDirectory'],
               spec['environment']['image'], task['probeTarget'], str(spec['budget']['checkTimeout'])]
        env = scoped_git_env([Path(snapshot), workspace])
        env.pop('PYTHONPATH', None)
        run = subprocess.run(cmd, cwd=snapshot, capture_output=True, timeout=280, env=env)
        for name, raw in (('stdout.log', run.stdout), ('stderr.log', run.stderr)):
            (directory / name).write_bytes(Redactor().output(raw))
        if run.returncode:
            return {'state': 'blocked', 'reason': 'probe_launcher_failed', 'stage': 'launcher',
                    'launcherExitCode': run.returncode, 'log': file_ref(directory / 'stderr.log')}
        result_path = directory / 'result.json'
    result = load(result_path)
    prove_log(result)
    return {**classify_probe(result), 'evidence': file_ref(result_path)}


def preflight_task(task, protocol, directory):
    evidence = {'environment': {'state': 'pending'}, 'scoring': {'state': 'pending'}, 'execution': {}}
    if not compatible(task['conditions']['A'], task['conditions']['B']):
        return admission(task, evidence)
    if task['conditions']['A']['model'] != protocol['provider']:
        evidence['environment'] = {'state': 'blocked', 'reason': 'registered_provider_mismatch'}
        return admission(task, evidence)
    try:
        known = prove_environment(task, protocol)
        evidence['environment'] = {'state': 'verified', 'image': task['conditions']['A']['environment']['image']}
    except Exception as exc:
        evidence['environment'] = {'state': 'blocked', 'reason': Redactor().private(str(exc))[:500]}
        return admission(task, evidence)
    try:
        evidence['scoring'] = prove_scoring(task, known)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        evidence['scoring'] = {'state': 'unscored', 'reason': Redactor().private(str(exc))[:500]}
    if evidence['scoring']['state'] == 'verified' and task.get('probeTarget'):
        for arm in task['arms']:
            slot = directory / arm
            slot.mkdir()
            try:
                evidence['execution'][arm] = run_probe(task, arm, protocol, slot, Path(known[1]['workspace']))
            except Exception as exc:
                evidence['execution'][arm] = {'state': 'blocked', 'stage': 'probe',
                                              'reason': Redactor().private(str(exc))[:500]}
    return admission(task, evidence)


def archive_ledger(directory, attempt, arm_commit, budget):
    traces = list((directory / 'trace').glob('*.jsonl'))
    trace, = traces
    events = [json.loads(line) for line in trace.read_bytes().splitlines()]
    settlements = [event for event in events if event.get('type') == 'evaluation_budget_settlement']
    reports = [event for event in events if event.get('type') == 'model_usage']
    accounting = attempt['tokenAccounting']
    if (len(reports) != attempt['usageReports'] or accounting['hardLimitTokens'] != budget
            or accounting['inFlightReservedTokens'] != 0
            or sum(event['accountedTokens'] for event in settlements) != accounting['accountedTokens']
            or sum(event['unknownUsageAttempts'] for event in settlements) != accounting['unknownUsageAttempts']):
        raise ValueError('Token ledger does not match the archived request settlements')
    archived = load(directory / 'agent-source/manifest.json')
    if archived['status'] != 'captured' or archived['git']['head'] != arm_commit:
        raise ValueError('Agent archive differs from the registered commit')
    for name, info in archived['files'].items():
        if digest((directory / 'agent-source/source' / name).read_bytes()) != info['sha256']:
            raise ValueError('Saved agent source artifact hash differs')
    return {'trace': reference(directory, trace), 'accounting': accounting, 'usage': attempt['usage'],
            'modelCalls': attempt['modelCalls'], 'httpAttempts': attempt['httpAttempts']}


def unscored(reason, **details):
    return {'status': 'UNSCORED', 'resolved': None, 'reason': reason, **details}


class SweBenchExecutor:
    def __init__(self, protocol, environment, worker_lock, *, scoring_runtime):
        self.protocol, self.env = protocol, environment
        self.worker_lock = worker_lock
        self.scoring_runtime = scoring_runtime

    def check_scoring(self):
        try:
            self.scoring_runtime.check()
        except ScoringError as exc:
            return unscored('scoring_runtime_not_verified', scoringFailure=True,
                            detail=Redactor().private(str(exc))[:500])

    def check_resources(self):
        with serial_slot(self.worker_lock):
            pass
        resources(self.protocol)

    def execute(self, task, arm, slot):
        try:
            known = prove_environment(task, self.protocol)
            if prove_scoring(task, known)['state'] != 'verified':
                return unscored('baseline_no_longer_trustworthy')
            return self._run_frozen(task, arm, slot)
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
            return unscored('execution_not_verified', detail=Redactor().private(str(exc))[:500],
                            **self._saved_usage(task, arm, slot))

    def _run_frozen(self, task, arm, slot):
        condition = task['conditions'][arm]
        if condition['model'] != self.protocol['provider']:
            raise ValueError('Task provider differs from the batch configuration')
        budget = condition['budget']
        snapshot = self.protocol['arms'][arm]
        verify_snapshot(snapshot)
        row = load(verify_file(task['dataset']))
        row['image'] = condition['environment']['image']
        save(slot, 'grading-input.json', row)
        # Git ownership exceptions are scoped to the exact already-verified source.
        old = dict(os.environ)
        scoped = scoped_git_env([task['source']])
        for key, value in scoped.items():
            if key.startswith('GIT_CONFIG_'):
                os.environ[key] = value
        try:
            directory = prepare(slot / 'grading-input.json', task['task'], task['source'],
                                workspaces_root=task.get('workspacesRoot'))
        finally:
            for key in scoped:
                if key.startswith('GIT_CONFIG_'):
                    if key in old:
                        os.environ[key] = old[key]
                    else:
                        os.environ.pop(key, None)
        directory = directory.resolve()
        save(slot, 'worker.json', {'runDirectory': str(directory)})
        launcher = Path(__file__).with_name('batch.py').resolve()
        command = [sys.executable, '-B', '-c', FROZEN_WORKER, str(launcher), str(self.worker_lock), 'run-agent',
                   '--run-dir', str(directory), '--baseline-report', task['baselineReport']['path'],
                   '--max-steps', str(budget['steps']), '--max-total-tokens', str(budget['tokens']),
                   '--max-seconds', str(budget['seconds']), '--context-tokens', str(budget['context']),
                   '--output-tokens', str(budget['output']), '--tokenizer', budget['tokenizer'],
                   '--agent-tests', '--check-image', condition['environment']['image'],
                   '--check-timeout', str(budget['checkTimeout'])]
        save(slot, 'command.json', {'argv': command, 'snapshot': snapshot['commit']})
        if failure := self.check_scoring():
            return {**failure, 'runDirectory': str(directory), 'usageUnknown': False}
        with (slot / 'model.log').open('xb') as log:
            run = subprocess.run(command, cwd=snapshot['snapshot'], stdout=log, stderr=log,
                                 env=scoped_git_env([task['source'], snapshot['snapshot']], self.env),
                                 timeout=budget['seconds'] + 300)
        if run.returncode:
            return unscored('model_worker_failed', processExit=run.returncode, **self._saved_usage(task, arm, slot))
        return self._score_or_pause(task, arm, slot)

    def recover(self, task, arm, slot, *, allow_grade=True):
        if not allow_grade:
            return unscored('recovery_interrupted', scoringFailure=True, **self._saved_usage(task, arm, slot))
        return self._score_or_pause(task, arm, slot)

    def _score_or_pause(self, task, arm, slot, *, force=False):
        try:
            return self._grade_saved(task, arm, slot, force=force)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            return unscored('scoring_not_verified', scoringFailure=True, detail=Redactor().private(str(exc))[:500],
                            **self._saved_usage(task, arm, slot))

    def _saved_usage(self, task, arm, slot):
        pointer = slot / 'worker.json'
        if not pointer.exists():
            return {'usageUnknown': (slot / 'command.json').exists()}
        directory = Path(load(pointer)['runDirectory'])
        try:
            ledger = archive_ledger(directory, load(directory / 'attempt.json'),
                                    self.protocol['arms'][arm]['commit'], task['conditions'][arm]['budget']['tokens'])
        except (OSError, ValueError, TypeError, KeyError):
            return {'usageUnknown': True, 'runDirectory': str(directory)}
        return {'ledger': ledger, 'runDirectory': str(directory)}

    def _grade_saved(self, task, arm, slot, *, force=False):
        directory = Path(load(slot / 'worker.json')['runDirectory'])
        _, run, prepared = load_run(directory)
        attempt = load(directory / 'attempt.json')
        config = load(directory / 'execution-config.json')
        condition = task['conditions'][arm]
        budget = condition['budget']
        if (prepared['taskId'], config['model'], config['profile'], config['checkImage'],
            config['maxTotalAccountedTokens'], config['maxSteps'], config['softMaxSeconds'],
            config['contextTokens'], config['outputTokens'], config['tokenizer'], config['checkTimeoutSeconds']) != (
            task['task'], condition['model']['model'], condition['model']['profile'],
            condition['environment']['image'], budget['tokens'], budget['steps'], budget['seconds'],
            budget['context'], budget['output'], budget['tokenizer'], budget['checkTimeout']):
            raise ValueError('Actual execution configuration differs from the registered pair')
        ledger = archive_ledger(directory, attempt, self.protocol['arms'][arm]['commit'], budget['tokens'])
        details = {'runDirectory': str(directory), 'ledger': ledger, 'execution': attempt['agentStatus'],
                   'patch': attempt.get('patch')}
        if not attempt.get('patch') or not read_reference(directory, attempt['patch']).strip():
            return unscored('no_candidate_patch', **details)
        row, = json.loads(read_reference(directory, run['gradingInput']))
        expected_row = load(verify_file(task['dataset']))
        if (run['sourceRepo'] != task['source'] or row != {**expected_row, 'image': condition['environment']['image']}
                or prepared['baseCommit'] != condition['environment']['baseCommit']):
            raise ScoringError('Saved patch uses a different grading input or source')
        git_env = scoped_git_env([task['source']])
        if git(task['source'], 'rev-parse', prepared['baseCommit'] + '^{tree}', env=git_env
               ).stdout.decode().strip() != condition['tree']:
            raise ScoringError('Pinned grading tree differs from registration')
        archive = {'patch': attempt['patch'], 'ledger': ledger,
                   'attempt': reference(directory, directory / 'attempt.json'),
                   'predictions': reference(directory, directory / 'predictions.jsonl'),
                   'gradingInput': run['gradingInput']}
        archived = slot / 'scoring-input.json'
        if archived.exists():
            if load(archived) != archive:
                raise ScoringError('Previously archived scoring inputs changed')
        else:
            save(slot, archived.name, archive)
        self.scoring_runtime.check()
        # Recover a complete existing grade, or grade the saved patch without a model.
        known = [] if force else [path for path in sorted(directory.glob('official/*/imported-grade.json'))
                                  if load(path).get('scoringRuntimeSha256') == self.scoring_runtime.identity]
        if known:
            pointer = load(known[-1])
            grader = known[-1].parent
        else:
            self.check_resources()
            grader = execute_grader(directory, timeout=budget['graderTimeout'],
                                    runtime=self.scoring_runtime, git_env=git_env)
            if not (grader / 'imported-grade.json').is_file():
                failure = load(grader / 'ungraded.json')
                return unscored('official_score_not_available', scoringFailure=True,
                                scoringReason=failure['reason'], grader=str(grader), **details)
            pointer = load(grader / 'imported-grade.json')
        grade = load(Path(pointer['gradeDirectory']) / 'grade.json')
        source = load(grader / 'source-verification.json')
        official = read_reference(Path(pointer['gradeDirectory']), grade['officialReport'])
        command = load(grader / 'command.json')
        grading_input = read_reference(grader, command['gradingInput'])
        predictions = read_reference(grader, command['predictions'])
        evidence = load(grader / 'score-evidence.json')
        reports = list((grader / 'logs').rglob('report.json'))
        self.scoring_runtime.check_files(grader)
        cleanup = [load(path)['status'] for path in grader.glob('*-cleanup.json')]
        if (source['imageId'] != condition['environment']['image'] or not source['sourceVerified']
                or source['baseCommit'] != prepared['baseCommit'] or source['expectedTree'] != condition['tree']
                or grade['patchSha256'] != attempt['patch']['sha256']
                or grade['taskId'] != task['task'] or grade['runId'] != run['runId']
                or pointer['actualGraderVersion'] != prepared['graderVersion']
                or command.get('scoringRuntimeSha256') != self.scoring_runtime.identity
                or grading_input != read_reference(directory, archive['gradingInput'])
                or predictions != read_reference(directory, archive['predictions'])
                or evidence.get('status') != 'consistent' or len(reports) != 1
                or digest(official) != evidence['report_sha256'] or reports[0].read_bytes() != official
                or digest(reports[0].with_name('test_output.txt').read_bytes()) != evidence['output_sha256']
                or digest(reports[0].with_name('patch.diff').read_bytes()) != attempt['patch']['sha256']
                or not cleanup or any(status not in {'removed', 'already_removed'} for status in cleanup)):
            return unscored('score_identity_mismatch', scoringFailure=True, grader=str(grader), **details)
        if grade['status'] not in {'pass', 'fail'} or type(grade['resolved']) is not bool:
            return unscored('official_score_not_verifiable', scoringFailure=True, grader=str(grader), **details)
        return {'status': 'RESOLVED' if grade['resolved'] else 'UNRESOLVED',
                'resolved': grade['resolved'], 'grader': str(grader),
                'scoringRuntimeSha256': self.scoring_runtime.identity, **details}
