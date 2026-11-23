"""Production benchmark entry point. Preflight and simulation never contact the Provider."""
import argparse
import json
from pathlib import Path
import shutil

from repo_agent.evaluation.artifacts import identifier, load_run, read_reference, save
from repo_agent.evaluation.batch import bound_input, load, run_queue, serial_slot
from repo_agent.evaluation.batch_swebench import (
    SweBenchExecutor, archive_ledger, file_ref, preflight_task, prove_environment, prove_scoring,
    provider_environment, resources, unscored, verify_file, verify_snapshot,
)
from repo_agent.evaluation.scoring_runtime import ScoringRuntime, freeze_runtime


CODE_FILES = ('batch.py', 'batch_swebench.py', 'benchmark.py', 'artifacts.py', 'container_lifecycle.py',
              'grader.py', 'official_runner.py', 'image_provider.py', 'runner.py', 'swebench.py',
              'scoring_runtime.py')


def code_identity():
    return {name: file_ref(Path(__file__).with_name(name))['sha256'] for name in CODE_FILES}


def registered(ref):
    protocol = bound_input(ref['path'], ref['sha256'])
    if protocol['schemaVersion'] != 1 or protocol['code'] != code_identity():
        raise ValueError('Production launcher differs from frozen protocol')
    samples = load(verify_file(protocol['sampleManifest']))['samples']
    tasks = protocol['tasks']
    if ([row['instance_id'] for row in samples] != [task['task'] for task in tasks]
            or len({task['task'] for task in tasks}) != len(tasks)
            or set(protocol['arms']) != {'A', 'B'}):
        raise ValueError('Task inventory does not match the registered sample order')
    return protocol


def preflight(registration, output):
    protocol = registered(registration)
    with serial_slot(output):
        for arm in protocol['arms'].values():
            verify_snapshot(arm)
        resources(protocol, verify_engine=True)
        binding = output / 'registration.json'
        if binding.exists():
            if load(binding) != registration:
                raise ValueError('Preflight folder is already bound to another protocol')
        else:
            save(output, binding.name, registration)
        if (output / 'release.json').exists():
            return output / 'release.json'
        scoring_runtime = freeze_runtime(output / identifier('scoring-runtime'), protocol['code'])
        decisions = {}
        for index, task in enumerate(protocol['tasks'], start=1):
            folder = output / 'pre' / str(index)
            decision = folder / 'admission.json'
            if decision.exists():
                row = load(decision)
            else:
                probe = folder / identifier('probe')
                probe.mkdir(parents=True)
                row = preflight_task(task, protocol, probe)
                save(folder, decision.name, row)
            decisions[task['task']] = row
            print(task['task'], row['state'], ', '.join(row['reasons']), flush=True)
        save(output, 'admissions.json', decisions)
        save(output, 'release.json', {'protocol': registration,
             'admission': file_ref(output / 'admissions.json'), 'scoringRuntime': scoring_runtime,
             'preflightModelCalls': 0})
    return output / 'release.json'


class Simulation:
    def check_resources(self):
        pass

    def execute(self, task, arm, slot):
        save(slot, 'simulated-execution.json', {'task': task['task'], 'arm': arm, 'modelCalls': 0})
        return {'status': 'SIMULATED', 'resolved': None, 'modelCalls': 0}

    def recover(self, task, arm, slot, *, allow_grade=True):
        return unscored('simulated_interruption', modelCalls=0)


def run_release(release_ref, output, *, start_limit, simulate):
    release = bound_input(release_ref['path'], release_ref['sha256'])
    protocol = registered(release['protocol'])
    evidence = release['admission']
    decisions = bound_input(evidence['path'], evidence['sha256'])
    if simulate:
        executor = Simulation()
    else:
        official = Path(evidence['path']).resolve().parent / 'official'
        if output != official:
            raise ValueError(f'Official execution must use its single registered output directory: {official}')
        for arm in protocol['arms'].values():
            verify_snapshot(arm)
        env, provider = provider_environment(Path(protocol['project']), protocol['arms'])
        if provider != protocol['provider']:
            raise ValueError('Current .env differs from the registered Provider identity')
        resources(protocol, verify_engine=True)
        scoring_runtime = ScoringRuntime(release['scoringRuntime'])
        executor = SweBenchExecutor(protocol, env, output / 'model-active', scoring_runtime=scoring_runtime)
    return run_queue(output, protocol, release_ref['sha256'], decisions, executor,
                     start_limit=start_limit, mode='simulation' if simulate else 'official')


def regrade_archive(release_ref, result_path, output):
    """Score a saved candidate in an independent evidence tree without dispatching a model."""
    release = bound_input(release_ref['path'], release_ref['sha256'])
    registration = release['protocol']
    protocol = bound_input(registration['path'], registration['sha256'])
    decision_ref = release['admission']
    decisions = bound_input(decision_ref['path'], decision_ref['sha256'])
    original_ref = file_ref(result_path)
    original = load(verify_file(original_ref))
    task, = (item for item in protocol['tasks'] if item['task'] == original['task'])
    arm = original['arm']
    if (original['mode'] != 'official' or original['protocolSha256'] != release_ref['sha256']
            or original['commit'] != protocol['arms'][arm]['commit']
            or decisions[task['task']]['state'] != 'READY'):
        raise ValueError('Archived result is outside the registered official cohort')
    source, record, _ = load_run(original['runDirectory'])
    if output.exists() or output.is_relative_to(source):
        raise ValueError('Supplemental scoring needs a new independent output directory')
    attempt = load(source / 'attempt.json')
    if not read_reference(source, attempt['patch']).strip():
        raise ValueError('Archived candidate is empty')
    budget = task['conditions'][arm]['budget']
    ledger = archive_ledger(source, attempt, original['commit'], budget['tokens'])
    if ledger != original['ledger']:
        raise ValueError('Archived ledger differs from the saved batch result')
    output.mkdir(parents=True, exist_ok=False)
    with serial_slot(output):
        resources(protocol, verify_engine=True)
        if prove_scoring(task, prove_environment(task, protocol))['state'] != 'verified':
            raise ValueError('Archived baseline no longer supports scoring')
        runtime = ScoringRuntime(freeze_runtime(output / 'runtime', code_identity()))
        run = output / 'r'
        names = ['run.json', 'attempt.json', 'predictions.jsonl', 'execution-config.json',
                 record['taskSpec']['path'], record['gradingInput']['path'],
                 attempt['patch']['path'], ledger['trace']['path']]
        for name in names:
            target = run / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        shutil.copytree(source / 'agent-source', run / 'agent-source')
        save(output, 'worker.json', {'runDirectory': str(run)})
        save(output, 'archive.json', {'originalResult': original_ref, 'originalRelease': release_ref,
                                     'scoringRuntimeSha256': runtime.identity, 'newModelCalls': 0})
        executor = SweBenchExecutor(protocol, None, output / 'model-active', scoring_runtime=runtime)
        outcome = executor._score_or_pause(task, arm, output, force=True)
        result = {**outcome, 'task': task['task'], 'arm': arm, 'commit': original['commit'], 'newModelCalls': 0}
        save(output, 'result.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    planning = commands.add_parser('preflight', help='Verify registered cases one at a time without a model')
    scheduling = commands.add_parser('simulate', help='Run the production queue without models or grading')
    actual = commands.add_parser('execute', help='Execute authorized real model arms and official grading')
    regrade = commands.add_parser('regrade', help='Score an archived candidate without calling any model')
    for command in (planning, scheduling, actual, regrade):
        command.add_argument('--input', type=Path, required=True)
        command.add_argument('--sha256', required=True)
        command.add_argument('--out', type=Path, required=True)
    scheduling.add_argument('--max-starts', type=int, required=True)
    actual.add_argument('--max-starts', type=int, required=True)
    regrade.add_argument('--archive-result', type=Path, required=True)
    args = parser.parse_args()
    ref = {'path': str(args.input.resolve()), 'sha256': args.sha256}
    output = args.out.resolve()
    if args.action == 'preflight':
        print(json.dumps({'release': file_ref(preflight(ref, output))}, ensure_ascii=False))
    elif args.action == 'regrade':
        result = regrade_archive(ref, args.archive_result.resolve(strict=True), output)
        print(json.dumps({'status': result['status'], 'reason': result.get('reason'),
                          'result': str(output / 'result.json')}, ensure_ascii=False))
        if result.get('scoringFailure'):
            raise SystemExit(2)
    else:
        result = run_release(ref, output, start_limit=args.max_starts, simulate=args.action == 'simulate')
        print(json.dumps({'mode': result['mode'], 'plannedTasks': result['plannedTasks'],
                          'admissionCounts': result['admissionCounts'], 'scoredPairs': result['scoredPairs'],
                          'scoringPause': result.get('scoringPause'),
                          'summary': str(output / 'summary.json')}, ensure_ascii=False))
        if result.get('scoringPause'):
            raise SystemExit(2)


if __name__ == '__main__':
    main()
