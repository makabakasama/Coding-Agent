"""Evaluation-only temporary Python checks, kept outside the business checkout."""
import json
import hashlib
from pathlib import Path
import re
import subprocess
import sys

from repo_agent.backend.services.task_execution_tools import TaskRepoTools
from repo_agent.core.tools import SKIP_DIRS, SKIP_FILES, TEXT_EXT
from repo_agent.core.check_tools import CHECK_PREFIX, CheckFiles, relative_file, review_check, run_independent_checks
from repo_agent.core.check_quality import evidence_sha256, verification_feedback, verification_summary
from repo_agent.core.provider import OpenAICompatibleProvider
from repo_agent.core import check_execution
from repo_agent.evaluation.artifacts import identifier, save, reference
from repo_agent.evaluation.swebench import git
from repo_agent.evaluation.test_targets import FrozenTestTargets
from repo_agent.evaluation.container_lifecycle import MEMORY_BYTES
from repo_agent.infrastructure.redaction import Redactor


class CheckProvider(OpenAICompatibleProvider):
    """Same decision protocol; accurate descriptions for evaluation-only routing."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.configure_development_checks(temporary=True)


class DevelopmentChecks:
    def __init__(self, directory, workspace, task, *, image, timeout=90, python=None, argv=None):
        if not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise ValueError('Developer checks require a preloaded immutable image ID')
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError('Check timeout must be between 1 and 300 seconds')
        self.directory, self.workspace, self.task = directory, workspace, task
        self.image, self.timeout, self.python = image, timeout, str(python or sys.executable)
        self.argv = check_execution.validate_check_argv(argv) if argv is not None else None
        self.targets = FrozenTestTargets(workspace, task['baseCommit'])
        self.root = directory / 'agent-checks'
        self.root.mkdir(exist_ok=False)
        self.files = CheckFiles(self.root, allow_writes=True)
        self.records = []
        self.review_cache = {}

    def _inventory(self):
        return self.files.inventory()

    def verification_summary(self, patch_sha256):
        return verification_summary(self.records, patch_sha256, evidence_sha256(self._inventory()),
                                    environment_sha256=self.environment_sha256())

    def refresh_verification_summary(self):
        return self.verification_summary(self.records[-1]['patchSha256'])

    def environment_sha256(self):
        return evidence_sha256({'image': self.image, 'python': self.python, 'timeout': self.timeout, 'argv': self.argv,
                                'base': self.task['baseCommit'],
                                'adapter': hashlib.sha256(Path(check_execution.__file__).read_bytes()).hexdigest()})

    def run(self, target, review=None):
        if not isinstance(target, str) or len(target) > 220:
            raise ValueError('Invalid focused test target')
        temporary = target.startswith(CHECK_PREFIX)
        if temporary:
            base = relative_file(target[len(CHECK_PREFIX):])
            if not base.endswith('.py') or not self.files._path(base).is_file():
                raise ValueError('Temporary check does not exist')
            test_bytes = self.files._path(base).read_bytes()
        else:
            test_bytes = self.targets.read(target)
        if not temporary and self.task['repo'] == 'sympy/sympy':
            check_execution.validate_sympy_selector(test_bytes, target)

        def read_frozen_repository(path):
            safe_path = relative_file(path)
            parts = safe_path.split('/')
            if (any(part.lower() in {name.lower() for name in SKIP_DIRS} for part in parts)
                    or parts[-1].lower() in {name.lower() for name in SKIP_FILES}
                    or parts[-1].lower().startswith('.env')
                    or Path(parts[-1]).suffix.lower() not in TEXT_EXT):
                raise ValueError('Repository evidence path is not a visible text file')
            result = git(self.workspace, 'show', self.task['baseCommit'] + ':' + safe_path)
            raw = result.stdout
            if len(raw) > 700_000:
                raise ValueError('Repository evidence file exceeds the readable size limit')
            return {'text': raw.decode('utf-8'), 'sha256': hashlib.sha256(raw).hexdigest()}

        review, evidence = review_check(self.review_cache, target, test_bytes, review,
                                        self.task['problemStatement'], read_frozen_repository)
        purpose = review['purpose'] if review else ('regression' if temporary else 'control')
        attempt = self.directory / 'development-checks' / identifier('check')
        attempt.mkdir(parents=True, exist_ok=False)
        patch = self.targets.export(attempt)
        # Snapshot checks before execution so later edits cannot rewrite old evidence.
        checks = attempt / 'checks'
        checks.mkdir()
        inventory = self._inventory()
        if temporary and next(item['sha256'] for item in inventory if item['path'] == base) != evidence['testSha256']:
            raise ValueError('Reviewed check changed before snapshot')
        staged_inventory = inventory if temporary else []
        for ref in staged_inventory:
            rel = ref['path']
            raw = self.files._path(rel).read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref['sha256']:
                raise ValueError('Temporary check changed while snapshotting')
            output = checks / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(raw)
        inventory_sha = evidence_sha256(inventory)
        environment_sha = self.environment_sha256()
        tree = git(self.workspace, 'rev-parse', self.task['baseCommit'] + '^{tree}').stdout.decode().strip()
        config = {'target': target, 'temporary': temporary, 'baseCommit': self.task['baseCommit'],
                  'tree': tree, 'repo': self.task['repo'], 'image': self.image,
                  'argv': self.argv,
                  'timeout': self.timeout, 'patch': patch, 'checks': staged_inventory,
                  'environmentSha256': environment_sha,
                  'compareBaseline': purpose == 'regression',
                  'reviewEvidence': evidence, 'purpose': purpose,
                  'containerConcurrency': 1, 'containerMemoryBytes': MEMORY_BYTES,
                  'containerCleanup': 'remove_after_output_archive',
            'officialTestsIncluded': False, 'modelWrittenChecksAreNotOfficialScores': True,
            'reviewReuseKey': {'target': target, 'testSha256': evidence['testSha256']}}
        save(attempt, 'request.json', config)
        launcher = attempt / 'check-runner.py'
        launcher.write_bytes(Path(__file__).with_name('check_runner.py').read_bytes())
        lifecycle = attempt / 'container_lifecycle.py'
        lifecycle.write_bytes(Path(__file__).with_name('container_lifecycle.py').read_bytes())
        adapter = attempt / 'check_execution.py'
        adapter.write_bytes(Path(check_execution.__file__).read_bytes())
        command = [self.python, str(launcher), str(attempt / 'request.json')]
        save(attempt, 'launch.json', {'command': command, 'launcher': reference(attempt, launcher),
                                    'containerLifecycle': reference(attempt, lifecycle),
                                    'checkExecution': reference(attempt, adapter)})
        redactor = Redactor()
        try:
            launcher_timeout = False
            completed = subprocess.run(command, capture_output=True, timeout=self.timeout + 180)
            stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:
            launcher_timeout = True
            stdout, stderr, exit_code = exc.stdout or b'', exc.stderr or b'', None
        except OSError as exc:
            stdout, stderr, exit_code = b'', str(exc).encode('utf-8'), None
        for name, raw in (('launcher-stdout.log', stdout), ('launcher-stderr.log', stderr)):
            (attempt / name).write_bytes(redactor.output(raw))
        result_file = attempt / 'result.json'
        if exit_code != 0 or not result_file.is_file():
            detail = redactor.output(stderr).decode('utf-8', errors='replace')[-4000:]
            receipt_file = attempt / 'execution.json'
            receipt = (json.loads(receipt_file.read_bytes()) if receipt_file.is_file() else
                       check_execution.execution_receipt(
                           None, '/testbed', stage='setup', timed_out=launcher_timeout,
                           error='Developer-check launcher failed before the test command',
                           environment={'image': self.image, 'fingerprint': environment_sha},
                           source={'baseCommit': self.task['baseCommit'], 'tree': tree, 'patchSha256': patch['sha256']},
                           logs={'status': 'partial', 'launcher': {'path': 'launcher-stderr.log'}}))
            result = {'passed': False, 'check_status': 'unknown', 'exit_code': receipt['exit_code'],
                    'execution': receipt, 'test_evidence': {'source': 'execution', 'granularity': 'none'},
                    'launcher_exit_code': exit_code,
                    'failure_stage': 'infrastructure', 'classification': {
                        'code': 'INFRA_ERROR',
                        'reason': detail.strip().splitlines()[-1] if detail.strip() else
                                  'Developer-check launcher timed out or did not produce a result',
                        'logs': {'launcher': 'launcher-stderr.log'}},
                    'error': 'Developer check infrastructure failed or timed out; inspect saved logs',
                    'launcher_stderr': detail,
                    'test_log': {'id': attempt.name, 'directory': str(attempt)},
                    'officialGrade': False}
            result = redactor.private_value(result)
            save(attempt, 'launcher-result.json', result)
        else:
            result = redactor.private_value(json.loads(result_file.read_bytes()))
        for output_file in (attempt / 'baseline/test-output.txt', attempt / 'test-output.txt'):
            if output_file.exists():
                output = redactor.private(output_file.read_text(encoding='utf-8', errors='replace'))
                output_file.write_bytes(output.encode('utf-8'))
        for phase_root in (attempt, attempt / 'baseline'):
            for name in ('execution.json', 'report.xml'):
                evidence_file = phase_root / name
                if evidence_file.is_file():
                    evidence_file.write_bytes(redactor.output(evidence_file.read_bytes()))
        output = (attempt / 'test-output.txt').read_text(encoding='utf-8') if (attempt / 'test-output.txt').exists() else ''
        record = {**result, 'target': target, 'checkId': attempt.name,
                  'patchSha256': patch['sha256'], 'inventorySha256': inventory_sha,
                  'check_kind': result.get('check_kind') or
                                ('model_authored_python' if temporary else 'existing_repository_tests'),
                  'environmentSha256': environment_sha,
                  'reviewEvidence': evidence}
        self.records.append(record)
        summary = verification_summary(self.records, patch['sha256'], inventory_sha,
                                       environment_sha256=environment_sha)
        save(attempt, 'verification.json', summary)
        baseline_file = attempt / 'baseline/test-output.txt'
        baseline_output = baseline_file.read_text(encoding='utf-8') if baseline_file.exists() else ''
        classification = record.get('classification', {})
        log_paths = {key: name for key, name in classification.get('logs', {}).items()
                     if name and (attempt / name).is_file()}
        feedback_evidence = {key: value for key, value in evidence.items() if key != 'criterionChecks'}
        return {**record, 'reviewEvidence': feedback_evidence,
                'verification': verification_feedback(summary, target), 'stdout': output[:12_000],
                'output_tail': output[-4000:] if len(output) > 12_000 else '',
                'baseline_stdout': baseline_output[:6000],
                'output_excerpt_truncated': len(output) > 12_000,
                'test_log': {'id': attempt.name, 'directory': str(attempt), 'files': log_paths},
                'officialGrade': False}


class EvaluationTools(TaskRepoTools):
    def __init__(self, *args, checks, **kwargs):
        super().__init__(*args, **kwargs)
        self.checks = checks
        checks.targets = FrozenTestTargets(self.root, checks.task['baseCommit'],
                                           authorized=self.contract['checkTargets'])
        self.require_check_before_completion = self.allow_tests
        if (self.root / CHECK_PREFIX.rstrip('/')).exists():
            raise ValueError('Temporary-check virtual namespace collides with repository files')

    def editable(self, path):
        return (super().editable(path)
                and not self.checks.targets.protects(self._path(path).relative_to(self.root).as_posix()))

    def run_tests(self, target='.', review=None, independent_targets=None):
        if independent_targets:
            return run_independent_checks(self.run_tests, target, independent_targets, review)
        if not self.allow_tests:
            raise PermissionError('Developer checks are disabled in this run')
        if getattr(self, 'policy_check', lambda: True)() is not True:
            raise PermissionError('Workspace identity audit failed')
        return self.checks.run(target, **({'review': review} if review is not None else {}))

