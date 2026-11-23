"""Serial, restartable scheduling for independently admitted benchmark pairs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

from repo_agent.evaluation.artifacts import digest


PAIR_FIELDS = {'taskRevision', 'tree', 'environment', 'model', 'budget', 'evaluator', 'toolPolicy'}
STATES = {'READY', 'UNSCORED', 'BLOCKED_INFRA', 'PENDING_PREFLIGHT'}


def load(path):
    return json.loads(Path(path).read_bytes())


def compatible(first, second):
    return (set(first) == PAIR_FIELDS == set(second)
            and all(value is not None for value in first.values()) and first == second)


def classify_probe(result, *, expected_exit=0):
    receipt = result.get('execution') or {}
    code = receipt.get('exit_code', result.get('exit_code'))
    stage = result.get('failure_stage')
    archive = result.get('output_archive') or receipt.get('logs', {}).get('archive') or {}
    complete = archive.get('status') == 'archived' or receipt.get('logs', {}).get('status') == 'complete'
    blocked = (result.get('classification', {}).get('code') == 'INFRA_ERROR'
               or stage in {'startup', 'infrastructure'} or receipt.get('timed_out')
               or receipt.get('status') == 'not_started')
    return {'state': 'blocked' if blocked else 'verified' if code == expected_exit and complete else 'pending',
            'exitCode': code, 'testEvidence': result.get('check_status'),
            'stage': stage, 'reason': result.get('classification', {}).get('reason') or result.get('error'),
            'logComplete': complete}


def admission(task, evidence):
    first, second = [task['conditions'][arm] for arm in task['arms']]
    probes = evidence['execution']
    environment, scoring = evidence['environment'], evidence['scoring']
    reasons = []
    if not compatible(first, second):
        reasons.append('pair_condition_mismatch')
    if environment['state'] == 'blocked':
        reasons.append(environment['reason'])
    reasons.extend(f'{arm}:{probe.get("stage") or "execution_blocked"}'
                   for arm, probe in probes.items() if probe['state'] == 'blocked')
    if reasons:
        state = 'BLOCKED_INFRA'
    elif scoring['state'] == 'unscored':
        state = 'UNSCORED'
        reasons.append(scoring['reason'])
    elif (environment['state'] == scoring['state'] == 'verified' and
          all(probes.get(arm, {}).get('state') == 'verified' for arm in task['arms'])):
        state = 'READY'
    else:
        state = 'PENDING_PREFLIGHT'
        reasons.extend(f'{arm}:execution_evidence_pending' for arm in task['arms']
                       if probes.get(arm, {}).get('state') != 'verified')
        if environment['state'] != 'verified':
            reasons.append('environment_evidence_pending')
        if scoring['state'] != 'verified':
            reasons.append('official_score_evidence_pending')
    return {**evidence, 'task': task['task'], 'state': state, 'reasons': reasons}


@contextmanager
def serial_slot(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'batch.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def bound_input(path, sha256):
    raw = Path(path).read_bytes()
    if digest(raw) != sha256:
        raise ValueError(f'Frozen input digest differs: {Path(path).name}')
    return json.loads(raw)


def publish(directory, name, value):
    """Publish a completed JSON file atomically underneath the serial slot."""
    temporary = directory / (name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(directory / name)


def attempt_path(root, index, arm):
    return root / 'attempts' / f'{index:04d}' / arm


def run_queue(root, protocol, protocol_sha, decisions, executor, *, start_limit, mode):
    """Resume by slot identity. An existing intent may only be recovered without dispatch."""
    tasks = protocol['tasks']
    arms = set(protocol['arms'])
    if (arms != {'A', 'B'} or len({task['task'] for task in tasks}) != len(tasks)
            or any(set(task['arms']) != arms or len(task['arms']) != 2 for task in tasks)
            or set(decisions) != {task['task'] for task in tasks}):
        raise ValueError('Batch task, arm or admission inventory differs from registration')
    for task in tasks:
        row = decisions[task['task']]
        if row['state'] not in STATES or row != admission(task, {
                key: row[key] for key in ('environment', 'execution', 'scoring')}):
            raise ValueError('Admission does not match its evidence')
    if type(start_limit) is not int or not 0 <= start_limit <= 2 * len(tasks):
        raise ValueError('Invalid explicit arm start limit')
    binding = {'protocolSha256': protocol_sha, 'mode': mode}
    with serial_slot(root):
        if (root / 'binding.json').exists():
            if load(root / 'binding.json') != binding:
                raise ValueError('Run directory is bound to a different batch or mode')
        else:
            publish(root, 'binding.json', binding)
        gate = root / 'scoring-pause.json'
        if gate.exists():
            return paused_summary(root, protocol, decisions, binding, load(gate))
        starts = 0
        for index, task in enumerate(tasks, start=1):
            if decisions[task['task']]['state'] != 'READY':
                continue
            slots = [(arm, attempt_path(root, index, arm)) for arm in task['arms']]
            new_slots = sum(not (slot / 'intent.json').exists() for _, slot in slots)
            can_start_pair = starts + new_slots <= start_limit
            for arm, slot in slots:
                identity = slot_identity(binding, protocol, task, arm)
                intent, result = slot / 'intent.json', slot / 'result.json'
                if intent.exists():
                    if load(intent) != identity:
                        raise ValueError('Existing arm intent does not match its registered slot')
                    if result.exists():
                        previous = load(result)
                        if previous.get('scoringFailure'):
                            return paused_summary(root, protocol, decisions, binding, previous)
                        continue
                    executor.check_resources()
                    recovery = slot / 'recovery.json'
                    if recovery.exists():
                        outcome = executor.recover(task, arm, slot, allow_grade=False)
                    else:
                        publish(slot, recovery.name, {'modelAllowed': False})
                        outcome = executor.recover(task, arm, slot)
                elif not can_start_pair:
                    continue
                else:
                    executor.check_resources()
                    if check := getattr(executor, 'check_scoring', None):
                        if failure := check():
                            return paused_summary(root, protocol, decisions, binding, {**identity, **failure})
                    slot.mkdir(parents=True, exist_ok=True)
                    publish(slot, intent.name, identity)
                    starts += 1
                    outcome = executor.execute(task, arm, slot)
                publish(slot, result.name, {**outcome, **identity})
                if outcome.get('scoringFailure'):
                    return paused_summary(root, protocol, decisions, binding, {**outcome, **identity})
        summary = summarize(root, protocol, decisions, binding)
        publish(root, 'summary.json', summary)
        return summary


def paused_summary(root, protocol, decisions, binding, failure):
    pause = {key: failure[key] for key in ('task', 'arm', 'reason', 'scoringReason', 'scoringFailure') if key in failure}
    publish(root, 'scoring-pause.json', pause)
    summary = summarize(root, protocol, decisions, binding)
    summary['scoringPause'] = pause
    publish(root, 'summary.json', summary)
    return summary


def slot_identity(binding, protocol, task, arm):
    return {**binding, 'task': task['task'], 'arm': arm, 'commit': protocol['arms'][arm]['commit']}


def summarize(root, protocol, decisions, binding):
    rows = []
    for index, task in enumerate(protocol['tasks'], start=1):
        outcomes = {}
        for arm in task['arms']:
            path = attempt_path(root, index, arm) / 'result.json'
            outcome = load(path) if path.exists() else {'status': 'NOT_STARTED', 'resolved': None}
            if path.exists() and any(outcome.get(key) != value for key, value in
                                     slot_identity(binding, protocol, task, arm).items()):
                outcome = {'status': 'UNSCORED', 'resolved': None, 'reason': 'result_identity_mismatch',
                           'usageUnknown': True}
            outcomes[arm] = outcome
        rows.append({'task': task['task'], 'admission': decisions[task['task']]['state'], 'arms': outcomes})
    metrics = {}
    for arm in ('A', 'B'):
        outcomes = [row['arms'][arm] for row in rows]
        scored = [item for item in outcomes if type(item.get('resolved')) is bool
                  and item['status'] in {'RESOLVED', 'UNRESOLVED'}]
        resolved = sum(item['resolved'] for item in scored)
        metrics[arm] = {'scored': len(scored), 'resolved': resolved,
                        'scoreRate': resolved / len(scored) if scored else None,
                        'unscored': sum(item['status'] == 'UNSCORED' for item in outcomes),
                        'notStarted': sum(item['status'] == 'NOT_STARTED' for item in outcomes),
                        'accountedTokens': sum(item.get('ledger', {}).get('accounting', {}).get('accountedTokens', 0)
                                               for item in outcomes),
                        'unknownUsageArms': sum(item.get('usageUnknown', False) or
                                                item.get('ledger', {}).get('accounting', {}).get('unknownUsageAttempts', 0) > 0
                                                for item in outcomes)}
    paired = [row for row in rows if all(item['status'] in {'RESOLVED', 'UNRESOLVED'} and
                                       type(item['resolved']) is bool for item in row['arms'].values())]
    return {'mode': binding['mode'], 'plannedTasks': len(rows), 'tasks': rows, 'byArm': metrics,
            'scoredPairs': len(paired), 'pairWins': {arm: sum(row['arms'][arm]['resolved'] and
                not row['arms'][other]['resolved'] for row in paired) for arm, other in (('A','B'),('B','A'))},
            'admissionCounts': {state: sum(row['admission'] == state for row in rows) for state in sorted(STATES)}}
