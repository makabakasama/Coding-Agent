"""Read-only process inspection for explicit stale-execution recovery."""
from __future__ import annotations

import json
import os
import subprocess


def active_processes(paths, server_pid=None):
    if os.name == 'nt':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                   '[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $ErrorActionPreference="Stop"; Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress']
    else:
        command = ['ps', '-eo', 'pid=,ppid=,args=']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        output, _ = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError('Process inspection timed out')
    if process.returncode:
        raise RuntimeError('Process inspection unavailable')
    if os.name == 'nt':
        values = json.loads(output.decode('utf-8-sig'))
        values = values if isinstance(values, list) else [values]
        rows = [(int(v['ProcessId']), int(v['ParentProcessId']), v.get('CommandLine')) for v in values]
    else:
        rows = [(int(parts[0]), int(parts[1]), parts[2]) for line in output.decode().splitlines()
                if len(parts := line.strip().split(None, 2)) == 3]
    if not rows:
        raise RuntimeError('Empty process inventory')
    descendants = set()
    if server_pid:
        while True:
            added = {pid for pid, parent, _ in rows if parent == server_pid or parent in descendants}
            if added <= descendants:
                break
            descendants |= added
    targets = [str(p).replace('\\', '/').casefold() for p in paths]
    active = []
    for pid, _, commandline in rows:
        if pid in (0, 4, os.getpid(), process.pid, server_pid):
            continue
        if pid in descendants and not commandline:
            raise RuntimeError('Associated process identity unavailable')
        normalized = (commandline or '').replace('\\', '/').casefold()
        if pid in descendants or any(path in normalized for path in targets):
            active.append(pid)
    return sorted(active)
