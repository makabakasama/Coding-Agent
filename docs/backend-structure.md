# 后端结构

从 `repo_agent/backend/app.py` 开始阅读。这里读取配置，选择存储实现，组装 `TaskService`，创建 HTTP 服务。

普通任务执行通过持久队列与独立 Worker 解耦。`backend/worker.py` 启动默认 3 槽位的执行池，调度状态由 `repositories/task_queue.py` 保存，API 入队由 `services/queued_execution.py` 处理，运行方式见 [Worker 池](worker-pool.md)。

| 目录 | 职责 | 主要入口 |
| --- | --- | --- |
| `backend/routers` | 接收 HTTP 请求、检查来源和参数、调用服务、生成响应 | `task_http.py` |
| `backend/services` | 编排需求创建、工作副本准备、Agent 执行及实验启动 | `task_service.py`、`task_execution.py` |
| `backend/repositories` | 读写任务、需求、轮次和历史材料，兼容数据库与文件格式 | `database_task_store.py`、`project_task_store.py` |
| `backend/schemas` | 定义任务数据契约、字段校验和错误结构 | `task_contract.py` |
| `core` | Agent 循环、模型调用、仓库工具和工作副本操作 | `agent.py`、`provider.py`、`tools.py` |
| `experimentation` | 固定版本准备、实验运行、补丁导出和独立验收 | `experiment_runner.py` |
| `infrastructure` | 文件锁、JSON 读写、脱敏、路径迁移及源码证据 | `file_io.py`、`redaction.py`、`run_evidence.py` |
| `cli` | 命令行参数与客户端交互、历史导入 | `task_cli.py`、`import_task_history.py` |

普通请求沿 HTTP 路由、服务、仓储的顺序处理。仓储需要的文件工具位于 `infrastructure`，避免从任务执行或准备服务反向引用。执行服务调用 `core` 中的现有 Agent 和工具。实验流程单独位于 `experimentation`。

HTTP 服务继续使用现有标准库实现。新增接口时在 `routers` 中处理传输协议，在 `schemas` 中校验数据，在 `services` 中组织业务步骤，在 `repositories` 中实现读写。配置、仓储选择与启动参数集中在 `backend/app.py`。

## 启动与检查

```powershell
.venv/Scripts/python.exe -m repo_agent.backend.app --port 4173
.venv/Scripts/python.exe -m repo_agent.cli.task_cli --help
.venv/Scripts/python.exe -m repo_agent.cli.import_task_history --help
.venv/Scripts/python.exe -m pytest tests -q
```

根目录的 `task_http.py`、`task_cli.py` 和 `import_task_history.py` 只保留旧命令的转发入口。其他 Python 调用方使用新包路径，例如 `from repo_agent.backend.services.task_service import TaskService`。

目录重构保留任务字段、HTTP 路径、数据库表和历史材料布局。源码证据采集按明确的包目录清单读取 Python 文件，校验器同时接受旧的扁平源码清单。历史快照中的导入路径保持原样。
