"""Ordinary tasks operating on the registered repository's current files."""
import difflib
import hashlib
import subprocess
from pathlib import Path
import uuid

from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.task_execution import TaskExecution, git
from repo_agent.backend.services.task_preparation import TaskPreparation
from repo_agent.infrastructure.file_io import exclusive, now, write_json


class DirectPreparation(TaskPreparation):
    def _identity(self, source, workspace):
        if source != workspace or workspace.resolve() != workspace:
            raise ValueError('Direct workspace must be the registered repository')
        probe = subprocess.run(['git','-C',str(source),'rev-parse','--show-toplevel'],capture_output=True,timeout=15)
        if probe.returncode != 0:
            return {'source':str(source),'workspace':str(workspace),'head':None,'gitDir':None,'commonDir':None}
        if Path(probe.stdout.decode().strip()).resolve() != source:
            raise ValueError('Repository root changed')
        def absolute(option):
            value = Path(git(source, 'rev-parse', option).decode().strip())
            return str((value if value.is_absolute() else source / value).resolve())
        return {'source': str(source), 'workspace': str(workspace),
                'head': git(source, 'rev-parse', 'HEAD').decode().strip(),
                'gitDir': absolute('--git-dir'), 'commonDir': absolute('--git-common-dir')}

    def query(self, task_id, *, prepare=False):
        task = self.store.get(task_id)
        directory = self._path(self.store.directory(task_id) / 'preparation')
        with exclusive(directory.parent / 'preparation.lock') as acquired:
            if prepare:
                self.store.assert_active(task_id)
            if directory.exists():
                if task['contract'].get('sourceKind') == 'directory':
                    from repo_agent.infrastructure.file_io import read_json
                    record=read_json(directory/'record.json')
                    if record.get('taskId') != task_id or record.get('baseCommit') is not None or record.get('status')!='prepared':
                        raise ValueError('Invalid directory preparation record')
                    return record
                return self._read(directory, task_id)
            if not prepare:
                return {'schemaVersion': 1, 'taskId': task_id, 'status': 'not_prepared'}
            if not acquired:
                raise TaskError(409, 'PREPARATION_BUSY', '执行目录正在准备')
            source = self.projects.repository(task['contract']['projectId'], task['contract']['baseCommit'])
            identity = self._identity(source, source)
            if identity['head'] != task['contract']['baseCommit']:
                raise TaskError(409, 'WORKSPACE_UNAVAILABLE', '当前仓库 HEAD 与本轮固定 commit 不一致，请新建轮次')
            identifier = uuid.uuid4().hex
            record = {'schemaVersion': 1, 'taskId': task_id, 'status': 'prepared',
                      'preparationId': identifier, 'workspaceId': identifier,
                      'startedAt': now(), 'finishedAt': now(), 'baseCommit': identity['head'],
                      'isExecutionSandbox': False}
            directory.mkdir()
            write_json(directory / 'identity.json', identity)
            write_json(directory / 'record.json', record)
            return record


class DirectExecution(TaskExecution):
    def _review(self, workspace, identity, baseline, tools):
        review = super()._review(workspace, identity, baseline, tools)
        entries = []
        for relative in review['changedPaths']:
            if not tools.editable(relative):
                continue
            path = workspace / relative
            entries.append({'path': relative, 'before': baseline.get(relative),
                            'after': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None})
        write_json(self.baseline_directory.parent / 'undo-manifest.json',
                   {'taskId': self.direct_task['taskId'], 'baseCommit': identity['head'],
                    'complete': review['status'] == 'passed', 'files': entries})
        return review

    def _workspace(self, task):
        source = self.projects.repository(task['contract']['projectId'], task['contract']['baseCommit'])
        if task['contract'].get('targetFile') and not (source/task['contract']['targetFile']).is_file():
            raise TaskError(409,'WORKSPACE_UNAVAILABLE','所选目标文件已不存在，请重新选择目标')
        identity = self.preparation._identity(source, source)
        if identity['head'] != task['contract']['baseCommit']:
            raise TaskError(409, 'WORKSPACE_UNAVAILABLE', '当前仓库 HEAD 已变化，请新建轮次')
        return source, identity

    def start(self, task_id, value):
        self.direct_task = self.store.get(task_id)
        self.preparation.query(task_id, prepare=True)
        self.baseline_directory = self.store.directory(task_id) / 'execution/baseline'
        return super().start(task_id, value)

    def run_claimed(self, task_id, execution_id):
        self.direct_task = self.store.get(task_id)
        self.preparation.query(task_id, prepare=True)
        self.baseline_directory = self.store.directory(task_id) / 'execution/baseline'
        return super().run_claimed(task_id, execution_id)

    def _snapshot(self, workspace):
        if self.direct_task['contract'].get('targetFile'):
            paths = [self.direct_task['contract']['targetFile']]
        elif self.direct_task['contract'].get('sourceKind') == 'directory':
            from repo_agent.core.tools import SKIP_DIRS, SKIP_FILES
            import os
            paths=[]
            for base,dirs,files in os.walk(workspace,followlinks=False):
                dirs[:]=[name for name in dirs if name not in SKIP_DIRS and not (Path(base)/name).is_symlink()
                         and not any((Path(base)/name).resolve().is_relative_to(root) for root in self._runtime_paths(workspace))]
                paths.extend((Path(base)/name).relative_to(workspace).as_posix() for name in files
                             if name not in SKIP_FILES and not name.startswith('.env'))
        else:
            paths = git(workspace, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')
        snapshot, contents = {}, {}
        for relative in sorted(set(paths) - {''}):
            path = workspace / relative
            if path.resolve() != path or path.is_symlink():
                snapshot[relative] = 'LINK'
            elif path.is_file():
                raw = path.read_bytes()
                snapshot[relative] = hashlib.sha256(raw).hexdigest()
                contents[relative] = raw
        if not hasattr(self, 'initial_contents'):
            self.initial_contents = contents
            from repo_agent.backend.services.task_execution_tools import TaskRepoTools
            tools = TaskRepoTools(workspace, contract=self.direct_task['contract'],excluded_roots=self._runtime_paths(workspace))
            self.baseline_directory.mkdir()
            for relative, raw in contents.items():
                try:
                    editable = tools.editable(relative)
                except (ValueError, OSError):
                    editable = False
                if editable:
                    destination = self.baseline_directory / 'files' / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(raw)
            write_json(self.baseline_directory / 'manifest.json', snapshot)
        return snapshot

    def _git_metadata(self, workspace, identity):
        # A main checkout has a .git directory rather than a worktree pointer file.
        if identity['gitDir'] is None:
            return {}
        paths = {'head': Path(identity['gitDir']) / 'HEAD', 'index': Path(identity['gitDir']) / 'index',
                 'commonConfig': Path(identity['commonDir']) / 'config'}
        return {name: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                for name, path in paths.items()}

    def _patch(self, workspace, task, safe_paths, tools):
        patch, omitted = b'', []
        for relative in safe_paths:
            before = self.initial_contents.get(relative, b'')
            path = workspace / relative
            after = path.read_bytes() if path.is_file() else b''
            if len(before) > 800000 or len(after) > 800000 or b'\0' in before + after:
                omitted.append(relative)
                continue
            try:
                old, new = before.decode('utf-8'), after.decode('utf-8')
            except UnicodeError:
                omitted.append(relative)
                continue
            patch += ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                             for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile='a/' + relative if relative in self.initial_contents else '/dev/null',
                                 tofile='b/' + relative if path.is_file() else '/dev/null')).encode('utf-8')
        return patch, omitted
