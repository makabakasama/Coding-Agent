"""Opt-in development reload with graceful child shutdown."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from repo_agent.infrastructure.run_evidence import ENTRY_SOURCES, SOURCE_PACKAGES, SOURCE_ROOT


def source_version(extra_paths=()):
    paths = {path for package in SOURCE_PACKAGES for path in (SOURCE_ROOT / package).glob('*.py')}
    paths.update(SOURCE_ROOT / name for name in ENTRY_SOURCES)
    paths.update(Path(path) for path in extra_paths)
    result = []
    for path in sorted(paths):
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            result.append((str(path), None, None))
    return tuple(result)


def watch_reload(callback):
    """The child only exits when its supervisor requests a graceful restart."""
    value = os.environ.get('CODING_AGENT_RELOAD_STOP_FILE')
    if not value:
        return
    path = Path(value)
    def watch():
        while not path.exists():
            time.sleep(.2)
        callback()
    threading.Thread(target=watch, name='reload-request', daemon=True).start()


def run_development(module, arguments, *, extra_paths=()):
    version = source_version(extra_paths)
    with tempfile.TemporaryDirectory(prefix='coding-agent-reload-') as temporary:
        stop_file = Path(temporary) / 'stop'
        while True:
            stop_file.unlink(missing_ok=True)
            environment = dict(os.environ, CODING_AGENT_RELOAD_CHILD='1',
                               CODING_AGENT_RELOAD_STOP_FILE=str(stop_file))
            child = subprocess.Popen([sys.executable, '-m', module, *arguments], env=environment)
            restarting = False
            try:
                while child.poll() is None:
                    current = source_version(extra_paths)
                    if not restarting and current != version:
                        print('Source changed. Waiting for active requests/tasks before restarting.', flush=True)
                        stop_file.touch()
                        restarting = True
                    time.sleep(.5)
                if not restarting:
                    return child.returncode
                version = source_version(extra_paths)
                print('Previous process stopped. Loading updated code.', flush=True)
            except KeyboardInterrupt:
                stop_file.touch()
                print('Stopping. Waiting for active requests/tasks.', flush=True)
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    child.terminate()
                    child.wait()
                    return 130
