"""Run-local launcher for pinned SWE scoring; imports alone start no work.

Copied into each grading directory and run by the grading interpreter, which
owns the optional docker/swebench dependencies. No project imports are needed.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import runpy
import sys
from contextlib import contextmanager

if __package__:
    from .container_lifecycle import (MEMORY_BYTES, container_slot, limited_official_containers,
                                      require_idle, save)
    from .image_provider import resolve_verified_image
else:
    from container_lifecycle import (MEMORY_BYTES, container_slot, limited_official_containers,
                                     require_idle, save)
    from image_provider import resolve_verified_image


def audit_score(row, report, test_output):
    """Check whether the pinned scorer observed the cases behind its verdict."""
    try:
        from swebench.harness.constants import EvalType, ResolvedStatus
        from swebench.harness.grading import (_resolve_case, get_eval_tests_report,
                                               get_logs_eval, get_resolution_status)
        from swebench.harness.utils import make_test_spec

        if importlib.metadata.version('swebench') != '5.0.2':
            return {'status': 'not_checked', 'reason': 'grader_version_mismatch'}
        spec = make_test_spec(row)
        observed, found = get_logs_eval(spec, str(test_output))
        item = report[spec.instance_id]
        eval_type = EvalType(spec.eval_type)
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        return {'status': 'not_checked', 'reason': 'grader_log_audit_unavailable'}
    if not item.get('patch_successfully_applied'):
        return {'status': 'not_checked', 'reason': 'patch_application_unconfirmed'}
    if not found:
        return {'status': 'unscored', 'reason': 'missing_scorable_test_output'}
    expected = {'FAIL_TO_PASS': spec.FAIL_TO_PASS, 'PASS_TO_PASS': spec.PASS_TO_PASS}
    if eval_type == EvalType.PASS_AND_FAIL:
        missing = {group: sum(_resolve_case(case, observed) is None for case in cases)
                   for group, cases in expected.items()}
        if any(missing.values()):
            return {'status': 'unscored', 'reason': 'required_case_outcomes_missing', 'missing': missing}
    recomputed = get_eval_tests_report(observed, expected, eval_type=eval_type)
    recorded = item.get('tests_status')
    for group in expected:
        group_result = recorded.get(group) if isinstance(recorded, dict) else None
        for outcome in ('success', 'failure'):
            cases = group_result.get(outcome) if isinstance(group_result, dict) else None
            if (not isinstance(cases, list) or any(not isinstance(case, str) for case in cases)
                    or sorted(cases) != sorted(recomputed[group][outcome])):
                return {'status': 'unscored', 'reason': 'report_disagrees_with_test_output'}
    resolved = get_resolution_status(recomputed) == ResolvedStatus.FULL.value
    if item.get('resolved') is not resolved:
        return {'status': 'unscored', 'reason': 'report_disagrees_with_test_output'}
    return {'status': 'consistent'}


@contextmanager
def linux_text_artifacts(*, windows=None):
    """Preserve LF for the two files copied to Linux, restore even on failure."""
    original = Path.write_text
    windows = os.name == 'nt' if windows is None else windows

    def write_text(path, data, encoding=None, errors=None, newline=None):
        if windows and path.name in {'eval.sh', 'patch.diff'}:
            encoding, newline = 'utf-8', '\n'
        return original(path, data, encoding=encoding, errors=errors, newline=newline)

    Path.write_text = write_text
    try:
        yield
    finally:
        Path.write_text = original


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-base', required=True)
    parser.add_argument('--expected-tree', required=True)
    parser.add_argument('--dataset_name', required=True)
    parser.add_argument('--instance_ids', required=True)
    parser.add_argument('--max_workers', type=int, default=1)
    args, official_args = parser.parse_known_args(argv)
    if args.max_workers != 1:
        raise ValueError('Evaluation container concurrency must be 1')
    if importlib.metadata.version('swebench') != '5.0.2':
        raise ValueError('This launcher requires swebench==5.0.2')
    for value in (args.expected_base, args.expected_tree):
        if not re.fullmatch(r'[a-f0-9]{40}', value):
            raise ValueError('Expected full commit and tree SHA values')
    source = Path(args.dataset_name).resolve(strict=True)
    rows = json.loads(source.read_bytes())
    if (not isinstance(rows, list) or len(rows) != 1
            or rows[0].get('instance_id') != args.instance_ids
            or rows[0].get('base_commit') != args.expected_base):
        raise ValueError('Expected exactly the frozen task and base commit')
    evidence = Path.cwd()
    import docker
    client = docker.from_env(timeout=120)
    try:
        require_idle(client)
        version = client.version()
        save(evidence / 'docker-environment.json', {
            key: version.get(key) for key in ('Version', 'ApiVersion', 'Os', 'Arch', 'KernelVersion')})
        effective = resolve_verified_image(client, rows[0], args.expected_base,
                                           args.expected_tree, evidence)
    finally:
        client.close()
    effective_path = evidence / 'effective-grading-input.json'
    save(effective_path, [effective])
    save(evidence / 'launcher-inputs.json', {
        'originalSha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'effectiveSha256': hashlib.sha256(effective_path.read_bytes()).hexdigest(),
        'launcherSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'imageProviderSha256': hashlib.sha256(
            Path(__file__).with_name('image_provider.py').read_bytes()).hexdigest(),
        'containerConcurrency': 1, 'containerMemoryBytes': MEMORY_BYTES,
        'containerCleanup': 'remove_after_recording',
        'windowsLFHandling': os.name == 'nt', 'officialPackagePatched': False})
    sys.argv = ['swebench.harness.run_evaluation', '--dataset_name', str(effective_path),
                '--instance_ids', args.instance_ids, *official_args, '--max_workers', '1']
    try:
        with linux_text_artifacts(), limited_official_containers(evidence, args.instance_ids):
            runpy.run_module('swebench.harness.run_evaluation', run_name='__main__')
    finally:
        artifacts = {}
        for path in (evidence / 'logs').rglob('*'):
            if path.is_file() and path.name in {'eval.sh', 'patch.diff'}:
                raw = path.read_bytes()
                artifacts[path.relative_to(evidence).as_posix()] = {
                    'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                    'crlfCount': raw.count(b'\r\n')}
        save(evidence / 'linux-artifacts.json', artifacts)
        reports = list((evidence / 'logs').rglob('report.json'))
        if len(reports) == 1:
            report, = reports
            output = report.with_name('test_output.txt')
            if output.is_file():
                raw = report.read_bytes()
                save(evidence / 'score-evidence.json',
                     {**audit_score(effective, json.loads(raw), output),
                      'report_sha256': hashlib.sha256(raw).hexdigest(),
                      'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest()})


def main(argv=None):
    with container_slot():
        return _main(argv)


if __name__ == '__main__':
    main()
