"""Content-verified scoring code and pinned local evaluator dependencies."""
import json
from pathlib import Path
import subprocess
import sys
import time

from repo_agent.evaluation.artifacts import digest, read_reference, save


SCORING_VERSION = 'scoring-gate.1'
LAUNCH_FILES = {'official_runner.py': 'official-runner.py',
                'container_lifecycle.py': 'container_lifecycle.py',
                'image_provider.py': 'image_provider.py'}
HOST_FILES = ('grader.py', 'swebench.py', 'artifacts.py', 'scoring_runtime.py')
ISOLATED = ('-I', '-B', '-X', 'utf8')
BOOT = ('import runpy,sys; from pathlib import Path; '
        'p=sys.argv.pop(1); sys.path.insert(0,str(Path(p).parent)); '
        'runpy.run_path(p,run_name="__main__")')
PROBE = '''
import hashlib,json,sys
from importlib.metadata import distributions, distribution
from pathlib import Path
from importlib.util import find_spec
def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()
exe = Path(sys.executable).resolve()
libs = [exe, *exe.parent.glob("python3*.dll")]
packages = sorted((d.metadata['Name'],d.version,
    hashlib.sha256((d.read_text('RECORD') or '').encode()).hexdigest()) for d in distributions())
critical = {}
for name in ('swebench', 'docker'):
    d = distribution(name)
    critical[name] = {str(f):sha(Path(d.locate_file(f))) for f in d.files
                      if f.suffix in {'.py','.pyd','.so'}}
print(json.dumps({'python':sys.version,'prefix':str(Path(sys.prefix).resolve()),
    'swebench':distribution('swebench').version, 'packages':packages, 'critical':critical,
    'origins':{n:find_spec(n).origin for n in critical}, 'binaries':{str(p):sha(p) for p in libs}}))
'''
STARTUP = ('import runpy,sys; from pathlib import Path; '
           'p=sys.argv[1]; sys.path.insert(0,str(Path(p).parent)); '
           'runpy.run_path(p,run_name="scoring_startup_probe"); '
           'import docker,swebench.harness.run_evaluation')


class ScoringError(RuntimeError):
    """The official result cannot be trusted or the scorer is unavailable."""


def inspect_runtime(python):
    try:
        run = subprocess.run([python, *ISOLATED, '-c', PROBE], capture_output=True, timeout=45, check=True)
        return json.loads(run.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ScoringError('Unable to inspect the isolated scoring interpreter') from exc


def freeze_runtime(folder, expected, *, python=None):
    folder = Path(folder).resolve()
    python = str(Path(python or sys.executable).resolve(strict=True))
    folder.mkdir(parents=True, exist_ok=False)
    files = {}
    for name, target in LAUNCH_FILES.items():
        raw = Path(__file__).with_name(name).read_bytes()
        if digest(raw) != expected[name]:
            raise ScoringError(f'Scoring source changed before freezing: {name}')
        path = folder / target
        path.write_bytes(raw)
        files[target] = {'path': target, 'sha256': expected[name], 'bytes': len(raw)}
    for name in HOST_FILES:
        if digest(Path(__file__).with_name(name).read_bytes()) != expected[name]:
            raise ScoringError(f'Scoring controller changed before freezing: {name}')
    environment = inspect_runtime(python)
    if environment['swebench'] != '5.0.2':
        raise ScoringError('Official scoring requires SWE-bench 5.0.2')
    try:
        subprocess.run([python, *ISOLATED, '-c', STARTUP, str(folder / 'official-runner.py')],
                       cwd=folder, capture_output=True, check=True, timeout=45)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = (getattr(exc, 'stderr', None) or b'').decode('utf-8', errors='replace').splitlines()
        raise ScoringError('Frozen scorer failed its import probe' + (': ' + detail[-1] if detail else '')) from exc
    ref = save(folder, 'manifest.json', {'version': SCORING_VERSION, 'python': python,
               'environment': environment, 'files': files, 'controller': {n: expected[n] for n in HOST_FILES}})
    return {**ref, 'path': str(folder / ref['path'])}


class ScoringRuntime:
    def __init__(self, ref):
        self.path = Path(ref['path']).resolve()
        self.ref = {**ref, 'path': self.path.name}
        self.manifest = json.loads(read_reference(self.path.parent, self.ref))
        if (self.manifest['version'] != SCORING_VERSION
                or set(self.manifest['files']) != set(LAUNCH_FILES.values())
                or set(self.manifest['controller']) != set(HOST_FILES)):
            raise ScoringError('Unknown or incomplete scoring runtime')
        self.python = self.manifest['python']
        self.identity = ref['sha256']

    def check_files(self, execution=None):
        try:
            read_reference(self.path.parent, self.ref)
            for ref in self.manifest['files'].values():
                read_reference(self.path.parent, ref)
                if execution is not None:
                    read_reference(execution, ref)
            for name, expected in self.manifest['controller'].items():
                if digest(Path(__file__).with_name(name).read_bytes()) != expected:
                    raise ValueError(f'Controller changed: {name}')
        except (OSError, ValueError) as exc:
            raise ScoringError(f'Scoring file drift: {exc}') from exc

    def check(self):
        self.check_files()
        if inspect_runtime(self.python) != self.manifest['environment']:
            raise ScoringError('Scoring interpreter or dependency fingerprint changed')

    def stage(self, directory):
        self.check()
        for name, ref in self.manifest['files'].items():
            (directory / name).write_bytes(read_reference(self.path.parent, ref))

    def command(self, argv):
        return [self.python, *ISOLATED, '-c', BOOT, *argv[1:]]

    def run(self, command, directory, timeout):
        self.check_files(directory)
        deadline = time.monotonic() + timeout
        drift = None
        with subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            while True:
                try:
                    stdout, stderr = child.communicate(timeout=max(0.001, min(2, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if drift is None:
                        try:
                            self.check_files(directory)
                        except ScoringError as exc:
                            drift = str(exc)
                    if time.monotonic() >= deadline:
                        child.kill()
                        stdout, stderr = child.communicate()
                        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        # Let the active evaluation exit through its container cleanup before quarantining.
        try:
            self.check_files(directory)
            self.check()
        except ScoringError as exc:
            drift = drift or str(exc)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr), drift
