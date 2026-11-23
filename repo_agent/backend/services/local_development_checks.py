"""Developer checks in the task's configured local test environment."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

from repo_agent.core.check_execution import classify_result, comparison_result
from repo_agent.core.check_quality import evidence_sha256, verification_feedback, verification_summary
from repo_agent.core.check_tools import CHECK_PREFIX, CheckFiles, relative_file, review_check
from repo_agent.core.tools import RepoTools, SKIP_DIRS, SKIP_FILES
from repo_agent.infrastructure.file_io import write_json
from repo_agent.infrastructure.redaction import Redactor


def visible_path(path):
    parts = relative_file(path).split('/')
    return not any(part.lower() in SKIP_DIRS for part in parts) and not (
        parts[-1].lower() in SKIP_FILES or parts[-1].lower().startswith('.env'))


def source_snapshot(root):
    result = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [folder for folder in folders if folder != '.git']
        for name in list(folders) + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == '.git':
                continue
            if path.resolve() != path:
                result[relative] = 'LINK'
                if name in folders:
                    folders.remove(name)
            elif path.is_file():
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class LocalDevelopmentChecks:
    def __init__(self, tools, task, directory, *, initial, snapshot, read_initial, redactor=None):
        self.tools, self.task, self.directory = tools, task, directory
        self.initial, self.snapshot, self.read_initial = initial, snapshot, read_initial
        self.redactor = redactor or Redactor()
        self.records, self.review_cache = [], {}
        self.files = None
        if task.get('allowTemporaryChecks', False):
            if (tools.root / CHECK_PREFIX.rstrip('/')).exists():
                raise ValueError('Temporary-check virtual namespace collides with repository files')
            directory.mkdir(parents=True, exist_ok=True)
            root = directory / 'agent-checks'
            root.mkdir(exist_ok=False)
            self.files = CheckFiles(root, allow_writes=True)

    def environment_sha256(self):
        python = Path(self.tools.test_python).resolve()
        stamp = python.stat()
        return evidence_sha256({'python': str(python), 'modified': stamp.st_mtime_ns, 'bytes': stamp.st_size,
                                'timeout': self.tools.test_timeout, 'argv': self.tools.test_argv,
                                'environment': {**os.environ, **self.tools.test_env}})

    def verification_summary(self, patch_sha256):
        inventory = self.files.inventory() if self.files else []
        return verification_summary(self.records, patch_sha256, evidence_sha256(inventory),
                                    environment_sha256=self.environment_sha256())

    def refresh_verification_summary(self):
        return self.verification_summary(evidence_sha256(self.snapshot()))

    def frozen_source(self, path):
        if not visible_path(path) or self.initial.get(path) in {None, 'LINK'}:
            raise ValueError('Repository evidence path is outside visible source')
        try:
            raw = self.read_initial(path)
        except subprocess.SubprocessError as exc:
            raise ValueError('Frozen repository evidence unavailable') from exc
        digest = hashlib.sha256(raw).hexdigest()
        if self.initial.get(path) != digest:
            raise ValueError('Repository evidence differs from the task start')
        return {'text': raw.decode('utf-8'), 'sha256': digest}

    @contextmanager
    def frozen_workspace(self):
        with tempfile.TemporaryDirectory(prefix='repo-agent-check-base-') as temporary:
            root = Path(temporary).resolve()
            if root.is_relative_to(self.tools.root):
                raise ValueError('Baseline check workspace must be outside the task checkout')
            for relative, digest in self.initial.items():
                if digest == 'LINK' or not visible_path(relative):
                    continue
                raw = self.read_initial(relative)
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError('Source differs from the task start')
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            yield root

    def baseline_check(self, target, scratch):
        try:
            with self.frozen_workspace() as root:
                tools = RepoTools(root, allow_tests=True, test_python=Path(self.tools.test_python),
                                  test_env={**self.tools.test_env, 'PYTHONPATH': str(root)},
                                  test_timeout=self.tools.test_timeout, test_log_dir=self.directory / 'baseline',
                                  test_output_filter=self.tools.test_output_filter, test_junit=self.tools.test_junit,
                                  test_argv=self.tools.test_argv)
                return tools.run_tests(target, scratch=scratch,
                                       source_version={'snapshot_sha256': evidence_sha256(self.initial)},
                                       environment_version=self.environment_sha256())
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            return {'passed': False, 'check_status': 'unknown', 'failure_stage': 'unknown',
                    'failure_detail': 'Frozen baseline unavailable: ' + self.redactor.private(str(exc))[:500],
                    'failure_evidence': []}

    def run(self, target, review=None):
        temporary = target.startswith(CHECK_PREFIX)
        inventory = self.files.inventory() if self.files else []
        if temporary:
            if self.files is None:
                raise PermissionError('Temporary Python checks are not authorized for this task')
            path = self.files._path(relative_file(target.removeprefix(CHECK_PREFIX)))
            if not any(entry['path'] == path.relative_to(self.files.root).as_posix() for entry in inventory):
                raise ValueError('Temporary check does not exist')
        else:
            path = self.tools._path(target.split('::', 1)[0])
        if path.is_dir():
            if review is not None:
                raise ValueError('Review must cite a specific test file')
            test_bytes = b''
        else:
            test_bytes = path.read_bytes()
        reader = None if self.task.get('targetFile') else self.frozen_source
        review, evidence = review_check(self.review_cache, target, test_bytes, review,
                                        self.task['current'] + '\n' + self.task['expected'], reader)
        purpose = review['purpose'] if review else 'regression' if temporary else 'control'
        current = self.snapshot()
        environment_sha = self.environment_sha256()
        changed_python = [path for path, digest in current.items()
                          if path.endswith('.py') and digest != self.initial.get(path)
                          and digest != 'LINK' and self.tools.editable(path)]
        baseline = None
        materials = {}
        with tempfile.TemporaryDirectory(prefix='repo-agent-check-scripts-') as staged:
            scratch = base_scratch = None
            if temporary:
                staging_root = Path(staged).resolve()
                if staging_root.is_relative_to(self.tools.root):
                    raise ValueError('Temporary check material must be outside the task checkout')
                scratch, base_scratch = staging_root / 'candidate', staging_root / 'base'
                for entry in inventory:
                    raw = self.files._path(entry['path']).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                        raise ValueError('Temporary check changed after snapshot')
                    if entry['path'] == target.removeprefix(CHECK_PREFIX) and entry['sha256'] != evidence['testSha256']:
                        raise ValueError('Reviewed check changed before snapshot')
                    materials[entry['path']] = raw
                    for phase_root in (scratch, base_scratch):
                        frozen = phase_root / entry['path']
                        frozen.parent.mkdir(parents=True, exist_ok=True)
                        frozen.write_bytes(raw)
            if purpose == 'regression':
                baseline = self.baseline_check(target, base_scratch)
            if getattr(self.tools, 'policy_check', lambda: True)() is not True:
                raise PermissionError('Workspace audit failed before candidate check')
            candidate = self.tools._run_authorized_check(
                target, scratch=scratch, changed_python=changed_python,
                source_version={'snapshot_sha256': evidence_sha256(current)},
                environment_version=environment_sha)
            if baseline is None and candidate.get('failure_stage') in {'assertion', 'test_execution'}:
                baseline = self.baseline_check(target, scratch)
        if self.snapshot() != current or self.environment_sha256() != environment_sha:
            candidate.update(passed=False, check_status='unknown', failure_stage='unknown',
                             failure_evidence_complete=False, check_reason='SOURCE_CHANGED_DURING_CHECK',
                             failure_detail='Source or execution environment changed during the check')
        comparison = comparison_result(baseline, candidate, purpose)
        if baseline and baseline.get('failure_detail'):
            comparison['baselineDetail'] = baseline['failure_detail']
        classification = classify_result(baseline, candidate)
        classification['logs'] = {
            phase: (result or {}).get('test_log', {}).get('id')
            for phase, result in (('candidate', candidate), ('baseline', baseline))
        }
        record = {**candidate, 'target': target, 'checkId': candidate.get('test_log', {}).get('id'),
                  'patchSha256': evidence_sha256(current), 'inventorySha256': evidence_sha256(inventory),
                  'environmentSha256': environment_sha,
                  'comparison': comparison, 'classification': classification,
                  'reviewEvidence': evidence, 'baseline': baseline}
        self.records.append(record)
        summary = self.verification_summary(record['patchSha256'])
        if record['checkId']:
            directory = Path(candidate['test_log']['directory'])
            for relative, raw in materials.items():
                if self.redactor.output(raw) != raw:
                    continue
                saved = directory / 'check-snapshot' / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(raw)
            payload = {'result': record, 'verification': summary, 'scratchInventory': inventory}
            write_json(directory / 'verification.json', self.redactor.private_value(payload))
        feedback_evidence = {key: value for key, value in evidence.items() if key != 'criterionChecks'}
        return self.redactor.private_value({**record, 'reviewEvidence': feedback_evidence,
                                            'verification': verification_feedback(summary, target)})
