import sys

import pytest

from repo_agent.evaluation.artifacts import digest
from repo_agent.evaluation.benchmark import code_identity
from repo_agent.evaluation.scoring_runtime import ScoringError, ScoringRuntime, freeze_runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from repo_agent.evaluation import scoring_runtime

    monkeypatch.setattr(scoring_runtime, 'inspect_runtime', lambda _: {'swebench': '5.0.2'})
    monkeypatch.setattr(scoring_runtime, 'STARTUP', 'pass')
    return ScoringRuntime(freeze_runtime(tmp_path / 'snapshot', code_identity()))


def test_snapshot_stages_verified_code_and_detects_dependency_change(tmp_path, runtime, monkeypatch):
    job = tmp_path / 'execution'
    job.mkdir()
    runtime.stage(job)
    for name, ref in runtime.manifest['files'].items():
        assert digest((job / name).read_bytes()) == ref['sha256']
    monkeypatch.setattr('repo_agent.evaluation.scoring_runtime.inspect_runtime',
                        lambda _: {'swebench': '5.0.2', 'packageSha256': 'changed'})
    with pytest.raises(ScoringError, match='dependency fingerprint'):
        runtime.check()


def test_runtime_detects_mutation_even_when_file_is_restored_before_child_exits(tmp_path, runtime):
    job = tmp_path / 'execution'
    job.mkdir()
    runtime.stage(job)
    target = job / 'image_provider.py'
    script = ('from pathlib import Path; import sys,time; p=Path(sys.argv[1]); '
              'original=p.read_bytes(); p.write_bytes(b"changed"); '
              'time.sleep(3); p.write_bytes(original)')
    command = [sys.executable, '-I', '-B', '-c', script, str(target)]
    completed, drift = runtime.run(command, job, 12)
    assert completed.returncode == 0
    runtime.check_files(job)
    assert 'Scoring file drift' in drift


def test_missing_packaged_module_blocks_stage(tmp_path, runtime):
    (runtime.path.parent / 'image_provider.py').unlink()
    job = tmp_path / 'execution'
    job.mkdir()
    with pytest.raises(ScoringError, match='file drift'):
        runtime.stage(job)
    assert not (job / 'official-runner.py').exists()


def test_startup_import_failure_cannot_produce_a_runtime_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr('repo_agent.evaluation.scoring_runtime.inspect_runtime',
                        lambda _: {'swebench': '5.0.2'})
    monkeypatch.setattr('repo_agent.evaluation.scoring_runtime.STARTUP',
                        'raise ModuleNotFoundError("image_provider")')
    folder = tmp_path / 'snapshot'
    with pytest.raises(ScoringError, match='failed its import probe.*image_provider'):
        freeze_runtime(folder, code_identity())
    assert not (folder / 'manifest.json').exists()
