"""Temporary check workspace and routing shared by coding and evaluation."""
import difflib
import hashlib
from copy import deepcopy
from pathlib import PurePosixPath
import subprocess

from repo_agent.core.tools import RepoTools
from repo_agent.core.decisions import CHECK_REVIEW_SCHEMA, INDEPENDENT_CHECK_TARGETS
from repo_agent.core.check_quality import review_evidence, verification_feedback


CHECK_PREFIX = '.agent-checks/'


def configure_check_tools(definitions, *, temporary):
    for tool in definitions:
        function = tool['function']
        if function['name'] == 'run_tests':
            function['description'] = (
                'Run an authorized existing repository check first. Report startup errors separately from '
                'test failures. Map each key task behavior to a concrete assertion in review. Include '
                'representative boundary and combination cases. Use exact named test targets for case '
                'evidence. Use expected_quote for literal evidence alongside a prose expectation. Record '
                'a possible oracle_conflict with a frozen repository citation. In gaps, name the most '
                'important untested behavior and what a focused check should establish. Use gap_id with '
                'the same requirement and scenario when supplying evidence to close it.'
                + (' Use .agent-checks/ for focused Python reproductions when repository checks leave a gap. '
                   'Django temporary checks normally use the repository test settings and initialized apps. '
                   'To initialize custom Django settings in the script, use # agent-check: custom-django-settings '
                   'as its first line.'
                   if temporary else ''))
            function['parameters']['properties']['review'] = deepcopy(CHECK_REVIEW_SCHEMA)
            function['parameters']['properties']['independent_targets'] = {
                **deepcopy(INDEPENDENT_CHECK_TARGETS),
                'description': 'Run these authorized targets separately even if the primary test fails.',
            }
        elif temporary and function['name'] == 'create_file':
            function['description'] += ' Use .agent-checks/<name>.py for authorized temporary checks.'


def review_check(cache, target, source, review, problem, reader):
    digest = hashlib.sha256(source).hexdigest()
    key = (target, digest)
    reused = review is None and key in cache
    if reused:
        review = cache[key]
    evidence = review_evidence(review, problem, source.decode('utf-8'), reader, target=target)
    if review is not None:
        cache[key] = review
    evidence.update(reused=reused, testSha256=digest)
    return review, evidence


def run_independent_checks(run, target, additional, review):
    targets = [target, *additional]
    if len(additional) > 2 or len(set(targets)) != len(targets):
        raise ValueError('Supply at most two distinct additional check targets')
    records = []
    verification = None
    for index, selected in enumerate(targets):
        try:
            result = run(selected, **({'review': review} if index == 0 and review is not None else {}))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result = {'passed': False, 'check_status': 'not_run', 'failure_stage': 'not_run',
                      'failure_detail': str(exc)}
        records.append({**result, 'target': selected})
        verification = result.get('verification') or verification
    if verification and 'behavioralCoverage' in verification:
        coverage = {key: value for key, value in verification['behavioralCoverage'].items()
                    if key != 'currentCheck'}
        coverage['byTarget'] = [
            {'target': record['target'], 'check_status': record.get('check_status'),
             'scenarios': ((record.get('verification') or {}).get('behavioralCoverage') or {}).get('currentCheck', [])}
            for record in records]
        verification = {**verification, 'behavioralCoverage': coverage}
    passed = all(result.get('passed') is True for result in records)
    first_failure = next((record for record in records if not record.get('passed')), {})
    return {'passed': passed, 'counts': None, 'check_status': 'passed' if passed else
            'failed' if any(result.get('check_status') == 'failed' for result in records) else 'unknown',
            'check_kind': 'existing_repository_tests' if all(
                result.get('check_kind') == 'existing_repository_tests' for result in records) else 'independent_batch',
            'check_reason': None if passed else 'One or more independent checks did not pass',
            'failure_detail': first_failure.get('failure_detail'),
            'classification': first_failure.get('classification'),
            'independent_results': records, 'verification': verification}


def relative_file(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(c in value for c in ('\n', '\r', '\0'))
            or PurePosixPath(value).is_absolute()
            or any(p in ('', '.', '..') or ':' in p or p.startswith('-') for p in value.split('/'))):
        raise ValueError('Expected a plain relative check/test path')
    return value


class CheckFiles(RepoTools):
    def inventory(self):
        inventory = []
        for path in sorted(self.root.rglob('*')):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = relative_file(path.relative_to(self.root).as_posix())
            raw = self._path(relative).read_bytes()
            if not relative.endswith('.py') or len(raw) > 60_000:
                raise ValueError('Invalid temporary check material')
            raw.decode('utf-8')
            inventory.append({'path': relative, 'bytes': len(raw),
                              'sha256': hashlib.sha256(raw).hexdigest()})
            if len(inventory) > 50 or sum(item['bytes'] for item in inventory) > 1_000_000:
                raise ValueError('Temporary check material budget exceeded')
        return inventory

    def _path(self, rel):
        if rel != '.':
            relative_file(rel)
        path = super()._path(rel)
        if path != (self.root / rel).absolute():
            raise ValueError('Linked check paths are excluded')
        return path

    def create_file(self, path, content):
        if not relative_file(path).endswith('.py'):
            raise ValueError('Temporary checks must be Python files')
        return super().create_file(path, content)

    def _apply_generated_edit(self, path, original, updated):
        # This directory sits under ignored runs/, inside the harness checkout.
        # Do not let `git apply` discover and operate on that outer repository.
        if original == updated:
            raise ValueError('Replacement makes no change')
        relative = path.relative_to(self.root).as_posix()
        before, after = original.encode('utf-8'), updated.encode('utf-8')
        if self._path(relative).read_bytes() != before:
            raise ValueError('Temporary check changed since its read snapshot')
        path.write_bytes(after)
        actual = path.read_bytes()
        self._expire_snapshots(relative)
        return {'path': relative, 'applied': True, 'consistent': actual == after,
                'before_sha256': hashlib.sha256(before).hexdigest(),
                'after_sha256': hashlib.sha256(actual).hexdigest(),
                'patch': ''.join(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
                            fromfile='a/' + relative, tofile='b/' + relative))}


class CheckToolRouting:
    def execute(self, action, args):
        if self.checks is None or self.checks.files is None:
            return super().execute(action, args)
        scratch = self.checks.files
        path_key = 'subdir' if action == 'list_files' else 'path'
        path = args.get(path_key, '')
        routed = action in {'read_file', 'create_file', 'search_code', 'list_files'} and (
            path == CHECK_PREFIX.rstrip('/') or path.startswith(CHECK_PREFIX))
        routed = routed or action == 'replace_lines' and args.get('snapshot_id') in scratch._snapshots
        if not routed:
            return super().execute(action, args)
        if getattr(self, 'policy_check', lambda: True)() is not True:
            return {'ok': False, 'error': 'Workspace identity audit failed'}
        forwarded = dict(args)
        if path:
            forwarded[path_key] = path[len(CHECK_PREFIX):] if path.startswith(CHECK_PREFIX) else '.'
        result = scratch.execute(action, forwarded)
        if result.get('ok'):
            data = result['data']
            for key in ('file', 'path', 'subdir'):
                if key in data and isinstance(data[key], str):
                    data[key] = CHECK_PREFIX + data[key]
            if 'files' in data:
                data['files'] = [CHECK_PREFIX + item for item in data['files']]
            for match in data.get('matches', []):
                match['file'] = CHECK_PREFIX + match['file']
            data['artifact_role'] = 'agent_check'
            if data.get('applied') and self.checks.records:
                data['verification'] = verification_feedback(self.checks.refresh_verification_summary())
        return result
