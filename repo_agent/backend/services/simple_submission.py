"""Translate a task description and local target into an explicit task contract."""
from pathlib import Path
import subprocess

from repo_agent.backend.schemas.task_contract import TaskError, _relative_path, validate_contract


class SimpleSubmission:
    def __init__(self, projects):
        self.projects = projects

    def normalize(self, value):
        allowed = {'instruction', 'targetPath', 'projectId', 'kind', 'maxSteps', 'workspaceMode',
                   'revision', 'allowedPaths', 'allowTests', 'checkTargets', 'expected'}
        if set(value) - allowed:
            raise TaskError(400, 'VALIDATION_ERROR', '简化任务包含不支持的字段')
        instruction = value.get('instruction')
        if not isinstance(instruction,str) or not instruction.strip():
            raise TaskError(400, 'VALIDATION_ERROR', '请填写任务描述')
        target_file = None
        if value.get('targetPath'):
            text = value['targetPath']
            if not isinstance(text,str) or not Path(text).expanduser().is_absolute():
                raise TaskError(400, 'INVALID_TARGET', '目标请填写本机文件或目录的绝对路径')
            try:
                target = Path(text).expanduser().resolve(strict=True)
            except (OSError,ValueError) as exc:
                raise TaskError(400, 'INVALID_TARGET', '目标文件或目录不存在') from exc
            if not target.is_file() and not target.is_dir():
                raise TaskError(400, 'INVALID_TARGET', '请选择普通文件或目录')
            if '.git' in target.parts:
                raise TaskError(400, 'INVALID_TARGET', 'Git 元数据不能作为任务目标')
            if target.is_file():
                from repo_agent.core.tools import TEXT_EXT
                if target.suffix.lower() not in TEXT_EXT:
                    raise TaskError(400, 'INVALID_TARGET', '当前文件类型不受代码工具支持，请选择源码或文本文件')
            directory = target.parent if target.is_file() else target
            probe = subprocess.run(['git','-C',str(directory),'rev-parse','--show-toplevel'],
                                   capture_output=True,text=True,encoding='utf-8',timeout=15)
            root = Path(probe.stdout.strip()).resolve() if probe.returncode == 0 else directory
            source_kind = 'git' if probe.returncode == 0 else 'directory'
            scope = target.relative_to(root).as_posix()
            if target.is_file():
                target_file = scope
            if scope != '.' and not _relative_path(scope):
                raise TaskError(400,'INVALID_TARGET','目标路径受保护或包含不支持的字符')
            project_id = 'pending-local'
        else:
            project_id = value.get('projectId')
            project = self.projects._projects().get(project_id) if isinstance(project_id,str) else None
            if project is None:
                raise TaskError(400,'INVALID_TARGET','请选择项目或填写本机目标路径')
            source_kind = project.get('sourceKind','git')
            scope = '.'
        kind = value.get('kind','add_feature')
        mode = value.get('workspaceMode','direct')
        if source_kind == 'directory' and mode != 'direct':
            raise TaskError(400,'INVALID_WORKSPACE_MODE','普通文件或目录使用直接模式，无需创建 Git 副本')
        contract = {'projectId':project_id,'sourceKind':source_kind,'kind':kind,'workspaceMode':mode,
                    'revision':value.get('revision') or ('HEAD' if source_kind=='git' else 'CURRENT'),
                    'current':instruction.strip(),'expected':value.get('expected') or '完成任务描述中的要求，说明实际修改、检查结果和未完成事项。',
                    'allowedPaths':[] if kind=='read_code' else value.get('allowedPaths') or [scope],
                    'allowTests':value.get('allowTests',False),'checkTargets':value.get('checkTargets',[]),
                    'maxSteps':value.get('maxSteps',50)}
        if target_file:
            contract['targetFile'] = target_file
        contract = validate_contract(contract)
        if target_file:
            if any(path != target_file for path in contract['allowedPaths']):
                raise TaskError(400,'INVALID_SCOPE','单文件任务的修改范围必须是所选文件')
        if value.get('targetPath'):
            contract['projectId'] = self.projects.register_local(root, source_kind)
        return contract
