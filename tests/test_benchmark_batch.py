from copy import deepcopy
from pathlib import Path

import pytest

from repo_agent.evaluation.artifacts import digest, save
from repo_agent.evaluation.batch import admission, bound_input, classify_probe, load, publish, run_queue
from repo_agent.evaluation.batch_swebench import archive_ledger


CONDITIONS = {'taskRevision': 'revision', 'tree': 'tree', 'environment': {'image': 'image'},
              'model': 'configured', 'budget': 500000, 'evaluator': 'official', 'toolPolicy': 'policy'}


def task(name, order=('A', 'B')):
    return {'task': name, 'arms': list(order), 'conditions': {arm: deepcopy(CONDITIONS) for arm in order}}


def decide(spec, *, score='verified', environment='verified', execution='verified'):
    return admission(spec, {'environment': {'state': environment, 'reason': 'environment unavailable'},
                            'scoring': {'state': score, 'reason': 'scoring unavailable'},
                            'execution': {arm: {'state': execution} for arm in spec['arms']}})


def protocol(*tasks):
    return {'arms': {'A': {'commit': 'first'}, 'B': {'commit': 'second'}}, 'tasks': list(tasks)}


class FakeExecution:
    def __init__(self, interrupt=None):
        self.interrupt = interrupt
        self.starts = []
        self.recoveries = []

    def check_resources(self):
        pass

    def execute(self, spec, arm, slot):
        self.starts.append((spec['task'], arm))
        save(slot, 'candidate.json', {'accountedTokens': 123, 'patchSha256': 'saved'})
        if self.interrupt == (spec['task'], arm):
            self.interrupt = None
            raise KeyboardInterrupt
        return {'status': 'RESOLVED' if arm == 'A' else 'UNRESOLVED', 'resolved': arm == 'A'}

    def recover(self, spec, arm, slot, *, allow_grade=True):
        if not allow_grade:
            return {'status': 'UNSCORED', 'resolved': None, 'ledger': load(slot / 'candidate.json')}
        self.recoveries.append((spec['task'], arm))
        return {'status': 'RESOLVED', 'resolved': True, 'ledger': load(slot / 'candidate.json')}


def test_only_ready_pairs_dispatch_in_registered_order_and_all_are_reported(tmp_path):
    unknown, ready, blocked, pending = (task(n) for n in ('no score', 'ready', 'blocked', 'pending'))
    other = task('other', ('B', 'A'))
    rows = [decide(unknown, score='unscored'), decide(ready), decide(blocked, environment='blocked'),
            decide(pending, execution='pending'), decide(other)]
    run = FakeExecution()
    result = run_queue(tmp_path, protocol(unknown, ready, blocked, pending, other), 'lock',
                       {row['task']: row for row in rows}, run, start_limit=4, mode='simulation')
    assert run.starts == [('ready','A'), ('ready','B'), ('other','B'), ('other','A')]
    assert result['plannedTasks'] == 5 and result['scoredPairs'] == 2
    assert result['byArm']['A']['scoreRate'] == 1
    assert result['byArm']['B']['notStarted'] == 3
    assert result['admissionCounts'] == {'BLOCKED_INFRA':1,'PENDING_PREFLIGHT':1,'READY':2,'UNSCORED':1}


@pytest.mark.parametrize('field', sorted(CONDITIONS))
def test_every_common_condition_drift_rejects_just_that_pair(tmp_path, field):
    changed = task('different')
    changed['conditions']['B'][field] = 'changed'
    good = task('good')
    rows = {'different': decide(changed), 'good': decide(good)}
    assert rows['different']['reasons'] == ['pair_condition_mismatch']
    run = FakeExecution()
    run_queue(tmp_path, protocol(changed, good), 'lock', rows, run, start_limit=2, mode='simulation')
    assert run.starts == [('good','A'), ('good','B')]


def test_unknown_developer_evidence_preserves_successful_execution():
    observed = {'check_status': 'unknown', 'exit_code': 0, 'output_archive': {'status': 'archived'}}
    result = classify_probe(observed)
    assert result['state'] == 'verified' and result['exitCode'] == 0 and result['testEvidence'] == 'unknown'
    assert classify_probe({**observed, 'output_archive': {'status': 'archive_failed'}})['state'] == 'pending'
    assert classify_probe({**observed, 'failure_stage': 'startup'})['state'] == 'blocked'


def test_interrupted_pair_recovers_saved_patch_and_only_starts_the_missing_arm(tmp_path):
    spec = task('recover')
    batch, rows = protocol(spec), {'recover': decide(spec)}
    runtime = FakeExecution(interrupt=('recover','A'))
    with pytest.raises(KeyboardInterrupt):
        run_queue(tmp_path, batch, 'hash', rows, runtime, start_limit=2, mode='simulation')
    evidence = tmp_path / 'attempts/0001/A/candidate.json'
    original = evidence.read_bytes()
    result = run_queue(tmp_path, batch, 'hash', rows, runtime, start_limit=1, mode='simulation')
    assert result['scoredPairs'] == 1
    assert result['tasks'][0]['arms']['A']['ledger']['accountedTokens'] == 123
    assert evidence.read_bytes() == original
    assert runtime.starts == [('recover','A'), ('recover','B')]
    assert runtime.recoveries == [('recover','A')]
    assert run_queue(tmp_path, batch, 'hash', rows, runtime, start_limit=0, mode='simulation') == result


def test_start_limit_does_not_create_half_pair_or_cross_modes(tmp_path):
    spec = task('pair')
    runtime = FakeExecution()
    args = (tmp_path, protocol(spec), 'hash', {'pair': decide(spec)}, runtime)
    result = run_queue(*args, start_limit=1, mode='simulation')
    assert result['byArm']['A']['notStarted'] == 1 and runtime.starts == []
    with pytest.raises(ValueError, match='different batch or mode'):
        run_queue(*args, start_limit=2, mode='official')


def test_interrupted_regrade_is_not_retried_and_keeps_prior_usage(tmp_path):
    class RecoveryInterrupt(FakeExecution):
        def recover(self, spec, arm, slot, *, allow_grade=True):
            if allow_grade:
                self.recoveries.append((spec['task'], arm))
                raise KeyboardInterrupt
            return {'status': 'UNSCORED', 'resolved': None, 'ledger': {
                'accounting': {**load(slot/'candidate.json'), 'unknownUsageAttempts': 1}}}

    spec = task('once')
    run = RecoveryInterrupt(interrupt=('once', 'A'))
    args = (tmp_path, protocol(spec), 'hash', {'once': decide(spec)}, run)
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            run_queue(*args, start_limit=2, mode='simulation')
    result = run_queue(*args, start_limit=1, mode='simulation')
    assert run.starts == [('once','A'), ('once','B')] and run.recoveries == [('once','A')]
    assert result['scoredPairs'] == 0
    assert result['byArm']['A']['accountedTokens'] == 123 and result['byArm']['A']['unknownUsageArms'] == 1


def test_mismatched_saved_arm_identity_is_never_compared(tmp_path):
    spec = task('scored')
    runtime = FakeExecution()
    args = (tmp_path, protocol(spec), 'hash', {'scored': decide(spec)}, runtime)
    run_queue(*args, start_limit=2, mode='simulation')
    folder = tmp_path / 'attempts/0001/A'
    stored = load(folder / 'result.json')
    publish(folder, 'result.json', {**stored, 'commit': 'not-the-registered-arm'})
    result = run_queue(*args, start_limit=0, mode='simulation')
    assert result['scoredPairs'] == 0
    assert result['tasks'][0]['arms']['A']['reason'] == 'result_identity_mismatch'
    assert runtime.starts == [('scored','A'), ('scored','B')]


def test_grader_failure_archives_the_arm_and_stops_dispatch_even_on_restart(tmp_path):
    class ScoringFailure(FakeExecution):
        def execute(self, spec, arm, slot):
            super().execute(spec, arm, slot)
            return {'status': 'UNSCORED', 'resolved': None, 'scoringFailure': True,
                    'reason': 'official_score_not_available', 'scoringReason': 'evaluator_failed',
                    'ledger': {'accounting': {'accountedTokens': 123}}}

    first, second = task('first'), task('second')
    executor = ScoringFailure()
    args = (tmp_path, protocol(first, second), 'hash',
            {spec['task']: decide(spec) for spec in (first, second)}, executor)
    result = run_queue(*args, start_limit=4, mode='official')
    assert executor.starts == [('first', 'A')]
    assert result['scoringPause']['scoringReason'] == 'evaluator_failed'
    assert result['byArm']['A']['unscored'] == 1
    assert result['byArm']['A']['accountedTokens'] == 123
    assert result['byArm']['B']['notStarted'] == 2
    assert not (tmp_path / 'attempts/0001/B/intent.json').exists()
    assert run_queue(*args, start_limit=4, mode='official') == result
    (tmp_path / 'scoring-pause.json').unlink()
    # A crash between publishing the failing result and the pause is also fail-closed.
    assert run_queue(*args, start_limit=4, mode='official') == result
    assert executor.starts == [('first', 'A')]


def test_unavailable_scoring_runtime_does_not_create_a_model_intent(tmp_path):
    class CannotStart(FakeExecution):
        def check_scoring(self):
            return {'status': 'UNSCORED', 'resolved': None, 'scoringFailure': True,
                    'reason': 'scoring_runtime_not_verified'}

    spec = task('first')
    executor = CannotStart()
    result = run_queue(tmp_path, protocol(spec), 'hash', {'first': decide(spec)},
                       executor, start_limit=2, mode='official')
    assert result['scoringPause']['reason'] == 'scoring_runtime_not_verified'
    assert result['byArm']['A']['notStarted'] == 1
    assert executor.starts == []
    assert not (tmp_path / 'attempts/0001/A/intent.json').exists()


def test_ledger_keeps_unknown_usage_and_checks_totals(tmp_path):
    trace = tmp_path / 'trace'
    trace.mkdir()
    (trace/'run.jsonl').write_text(
        '{"type":"model_usage"}\n'
        '{"type":"evaluation_budget_settlement","accountedTokens":12,"unknownUsageAttempts":0}\n'
        '{"type":"evaluation_budget_settlement","accountedTokens":20,"unknownUsageAttempts":1}\n',
        encoding='utf-8')
    archive = tmp_path / 'agent-source'
    (archive/'source').mkdir(parents=True)
    raw = b'unchanged'
    (archive/'source/agent.py').write_bytes(raw)
    save(archive, 'manifest.json', {'status':'captured','git':{'head':'pinned'},
         'files':{'agent.py':{'sha256':digest(raw)}}})
    accounted = {'accountedTokens':32,'unknownUsageAttempts':1,'hardLimitTokens':500000,
                 'inFlightReservedTokens':0}
    attempt = {'tokenAccounting':accounted,'usageReports':1,'usage':{},'modelCalls':2,'httpAttempts':2}
    assert archive_ledger(tmp_path, attempt, 'pinned', 500000)['accounting'] == accounted
    accounted['unknownUsageAttempts'] = 0
    with pytest.raises(ValueError, match='ledger'):
        archive_ledger(tmp_path, attempt, 'pinned', 500000)


def test_lock_digest_rejects_modified_frozen_registration(tmp_path):
    ref = save(tmp_path, 'protocol.json', {'complete': True})
    path = tmp_path / ref['path']
    assert bound_input(path, ref['sha256']) == {'complete': True}
    path.write_text('{}')
    with pytest.raises(ValueError, match='digest'):
        bound_input(path, ref['sha256'])
