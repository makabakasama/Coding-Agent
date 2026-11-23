"""Model-reported steps linked to program-recorded tool observations."""
from copy import deepcopy
import uuid


EDIT_ACTIONS = {'apply_patch', 'replace_text', 'replace_lines', 'create_file'}
CHECK_REMINDER_ACTIONS = 3


class RunProgress:
    def __init__(self):
        self.revision = 0
        self.reason = ''
        self.steps = []
        self.edit_revision = 0
        self.check_revision = 0
        self.last_test_passed = None
        self.actions_without_current_check = 0
        self.observations = {}
        self.developer_verification = None
        self.latest_check = None
        self.latest_check_kind = None
        self.latest_check_ids = set()
        self._run_id = uuid.uuid4().hex

    def observe(self, action, observation, *, target=None):
        data = observation.get('data', {})
        if action in EDIT_ACTIONS and observation['ok'] and data.get('applied'):
            if data.get('artifact_role') == 'agent_check':
                self.check_revision += 1
                refreshed = data.get('verification') or {}
                current_check_ids = {item.get('checkId') for item in refreshed.get('checks', [])}
                if (self.latest_check_kind == 'existing_repository_tests' and self.latest_check is not None
                        and self.latest_check['editRevision'] == self.edit_revision
                        and (not refreshed or (self.latest_check_ids and self.latest_check_ids <= current_check_ids))):
                    self.latest_check['checkRevision'] = self.check_revision
                else:
                    self.last_test_passed = None
                self.developer_verification = (
                    deepcopy(data.get('verification')) if self.latest_check
                    and self.latest_check['editRevision'] == self.edit_revision else None)
            else:
                self.edit_revision += 1
                self.last_test_passed = None
                self.developer_verification = None
        if action == 'run_tests':
            self.last_test_passed = data.get('passed') if observation['ok'] else False
            self.developer_verification = deepcopy(data.get('verification'))
            self.actions_without_current_check = 0
        elif self.edit_revision and self.last_test_passed is not True:
            self.actions_without_current_check += 1
        identifier = f'obs_{self._run_id}_{len(self.observations) + 1}'
        fact = {'id': identifier, 'action': action, 'ok': observation['ok'],
                'edit_revision': self.edit_revision, 'check_revision': self.check_revision}
        for key in ('path', 'file', 'paths', 'applied', 'consistent', 'before_sha256', 'after_sha256',
                    'sha256', 'passed', 'check_status', 'check_kind', 'exit_code', 'scope', 'subdir',
                    'complete', 'scan_complete', 'stop_reason', 'matched_lines_total', 'artifact_role',
                    'patchSha256', 'environmentSha256'):
            if key in data:
                fact[key] = deepcopy(data[key])
        log = data.get('test_log', {})
        if log.get('id'):
            fact['test_log_id'] = log['id']
        if action == 'run_tests':
            self.latest_check_kind = data.get('check_kind')
            self.latest_check_ids = {log['id']} if log.get('id') else set()
            self.latest_check_ids.update(result['checkId'] for result in data.get('independent_results', [])
                                         if result.get('checkId'))
            classification = data.get('classification') or {}
            self.latest_check = {
                'observationId': identifier, 'target': target,
                'editRevision': self.edit_revision, 'checkRevision': self.check_revision,
                'passed': self.last_test_passed, 'counts': deepcopy(data.get('counts')),
                'classification': classification.get('code'),
                'reason': data.get('failure_detail') or classification.get('reason')
                          or data.get('failure_kind') or observation.get('error'),
                'logId': log.get('id'),
            }
            if receipt := data.get('execution'):
                self.latest_check['execution'] = {key: receipt[key] for key in ('status', 'stage', 'exit_code')}
                self.latest_check['testEvidence'] = deepcopy(data.get('test_evidence'))
        self.observations[identifier] = fact
        return identifier

    def update(self, steps, reason):
        if not reason.strip():
            raise ValueError('A plan revision requires a nonempty reason')
        by_id = {step['id']: step for step in steps}
        if len(by_id) != len(steps):
            raise ValueError('Step IDs must be unique')
        if sum(step['status'] == 'in_progress' for step in steps) > 1:
            raise ValueError('At most one step may be in_progress')
        for previous in self.steps:
            if previous['id'] not in by_id or by_id[previous['id']]['goal'] != previous['goal']:
                raise ValueError('Keep existing step IDs and goals; reopen or append steps as needed')
        for step in steps:
            if not step['goal'].strip():
                raise ValueError('Step goals must be nonempty')
            refs = step['evidence_refs']
            if len(set(refs)) != len(refs) or any(ref not in self.observations for ref in refs):
                raise ValueError('Evidence must reference unique observations already recorded in this run')
            if step['status'] == 'completed' and not refs:
                raise ValueError('Completed steps require actual observation references')
            if step['status'] == 'blocked' and not step['blocked_reason'].strip():
                raise ValueError('Blocked steps require a concrete blocked_reason')
            if step['status'] != 'blocked' and step['blocked_reason']:
                raise ValueError('Clear blocked_reason when a step is no longer blocked')
        self.steps = deepcopy(steps)
        self.reason = reason
        self.revision += 1

    def _stale_refs(self):
        stale = set()
        current = self.developer_verification or {}
        for step in self.steps:
            if step['status'] != 'completed':
                continue
            for ref in step['evidence_refs']:
                fact = self.observations[ref]
                if fact.get('passed') is True and (fact['edit_revision'] != self.edit_revision
                        or (fact.get('check_kind') != 'existing_repository_tests'
                            and fact['check_revision'] != self.check_revision)
                        or any(fact.get(key) and current.get(key) and fact[key] != current[key]
                               for key in ('patchSha256', 'environmentSha256'))):
                    stale.add(ref)
        return sorted(stale)

    def snapshot(self):
        refs = {ref for step in self.steps for ref in step['evidence_refs']}
        return {'revision': self.revision, 'reason': self.reason, 'steps': deepcopy(self.steps),
                'current_step_id': next((step['id'] for step in self.steps if step['status'] == 'in_progress'), None),
                'remaining_step_ids': [step['id'] for step in self.steps if step['status'] != 'completed'],
                'stale_evidence_refs': self._stale_refs(), 'edit_revision': self.edit_revision,
                'check_revision': self.check_revision,
                'last_test_passed': self.last_test_passed,
                'evidence': [deepcopy(fact) for ref, fact in self.observations.items() if ref in refs],
                'steps_model_reported': True}

    def runtime_state(self, step, max_steps, *, require_check=False):
        progress = self.snapshot()
        progress.pop('evidence')
        state = {'used_decisions': step - 1, 'current_decision': step,
                'remaining_decisions': max_steps - step + 1, 'max_decisions': max_steps,
                'remaining_includes_current': True, 'progress': progress,
                'recent_observations': deepcopy(list(self.observations.values())[-3:])}
        if require_check:
            state['developer_verification'] = deepcopy(self.developer_verification)
            reminder = None
            if self.last_test_passed is False:
                reminder = ('The last developer check failed or could not run. Inspect its output, '
                            'repair the implementation/check when justified, and rerun. '
                            'If concretely blocked, report incomplete/blocked with the reason.')
            elif self.edit_revision and self.last_test_passed is not True:
                reminder = 'Business code changed. Run a focused developer check before claiming completion.'
                if self.actions_without_current_check >= CHECK_REMINDER_ACTIONS:
                    reminder = ('Several tool actions have followed code edits without a current passing check. '
                                'Prioritize creating/running a focused .agent-checks/ script or an existing test '
                                'rather than repeating reads/edits. If unable, report incomplete/blocked honestly.')
            state['verification_policy'] = {
                'required_for_completed': True,
                'current_check': 'passed' if self.last_test_passed is True else
                                 'failed' if self.last_test_passed is False else 'not_verified',
                'actions_without_current_check': self.actions_without_current_check,
                'reminder_after_actions': CHECK_REMINDER_ACTIONS, 'reminder': reminder,
                'official_grade': 'separate; developer checks do not establish benchmark success'}
        return state

    def completion_issues(self, outcome, *, require_check=False):
        if outcome not in ('completed', 'already_satisfied'):
            return []
        issues = []
        if self.last_test_passed is False:
            issues.append('The last observed check failed; repair it or finish incomplete/blocked')
        elif require_check and self.last_test_passed is not True:
            issues.append('No passing developer check exists for the current code/check versions. '
                          'Call run_tests on a focused check before claiming completion, '
                          'or report incomplete/blocked with the concrete reason.')
        if require_check and self.developer_verification is not None and self.developer_verification.get('level') in (
                'developer_check_failed', 'verification_incomplete'):
            issues.append('Current developer checks contain a failure or incomplete execution. '
                          'Inspect verification details, or finish incomplete/blocked.')
        if not self.steps:
            return issues
        remaining = [step['id'] for step in self.steps if step['status'] != 'completed']
        if remaining:
            issues.append('Unfinished steps: ' + ', '.join(remaining))
        if self._stale_refs():
            issues.append('Completed steps reference passing checks from an earlier edit revision')
        return issues
