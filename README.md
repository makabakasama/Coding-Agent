# Coding Agent — V0.4 本地编码工作台

面向学习和工程验证的 Coding Agent。输入本机项目、目录或文件路径及任务描述，由模型调用工具读取、搜索和修改代码，交付差异、检查结果、完成说明与运行记录。

**V0.4 当前范围：PostgreSQL 持久任务与独立 Worker 池、本机路径任务、直接修改与撤回、回收站、可选 Redis 读取缓存。** 本地单用户使用，不是多租户服务；工具执行仍在本机，没有 Docker 执行沙箱。

当前实现、检查结果与限制见 [V0.4 状态报告](docs/v0.4/status.md)。V0.3 和早期实验文档保留为历史记录，不作为当前启动说明。

## 已有能力

- **任务入口**：简化表单使用“本机路径 + 任务描述”，支持 Git 项目、子目录、普通目录和单文件；可选只读分析。高级表单保留阅读、修复和小功能三类契约。
- **执行方式**：简化入口默认直接修改目标、50 步、不执行测试；Git 目标也可选固定 commit 的独立 worktree。完整契约未指定模式时沿用 worktree。普通目录和单文件的非 Git 目标使用直接模式。
- **持久调度**：API 入队，独立 Worker 进程执行；默认 3 个线程槽位。重复提交同一轮不创建重复调度，相同根目录的直接任务串行执行。Worker 中断后保留材料并标记待核实，不自动续跑或重试。
- **修改与交付**：快照编辑核对文件哈希；直接模式保存执行前基线和执行后哈希，支持冲突检查后的显式撤回。结果分别展示模型停止原因、需求完成状态、实际检查和范围审查。
- **任务管理**：删除到回收站、恢复及二次确认后的彻底删除。删除记录不撤回源码修改；永久删除会清除该轮材料和经核对的专属 worktree。
- **缓存与开发**：可选 Redis 读取缓存、PostgreSQL 连接池；API/Worker 支持开发自动重载，Worker 等待在途任务交付后重启。
- **固定实验**：保留 G/R/P 修复流程与独立验收。历史 browser-use #5998、pydantic-ai #10525 两项已知答案实验有本机独立通过记录，不代表一般成功率或上游贡献。

详细操作见 [任务创建与回收站](docs/simple-tasks.md)、[Worker 与撤回](docs/worker-pool.md)、[数据库](docs/database-tasks.md)和 [Redis 缓存](docs/redis-cache.md)。

## 启动工作台

要求 Python 3.11+、Git、可用的 PostgreSQL 数据库；Redis 可选。目标项目的测试依赖需要单独准备。以下命令在项目根目录执行。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

在本机 `.env` 配置：

- `DATABASE_URL`：已创建数据库的连接地址，API 和 Worker 必需。连接用户需具备建表与读写权限。
- `LLM_PROVIDER=openai_compatible`、`LLM_PROFILE=gpt` 或 `qwen`，以及对应分组的 `MODEL`、`BASE_URL`、`API_KEY`。实际模型以配置为准，profile 只是分组标签；选中分组缺项不会借用另一分组。
- `REDIS_URL`：可选读取缓存；不使用时留空。Redis 不是任务队列，调度事实保存在 PostgreSQL。

分别在两个终端启动：

```powershell
# 终端一：API，本文统一使用 4176，匹配任务 CLI 的默认端口
.venv/Scripts/python.exe -m repo_agent.backend.app --port 4176
```

```powershell
# 终端二：独立 Worker，默认 3 个执行槽位
.venv/Scripts/python.exe -m repo_agent.backend.worker --workers 3
```

打开 [本地工作台](http://127.0.0.1:4176)，填写路径和描述，保存后显式提交执行。仅保存需求不调用模型；Worker 启动后会领取数据库中已有的排队任务，可能随即调用模型。直接模式会修改你选择的本机目标，隔离模式需先准备 worktree。

API 不传 `--port` 时默认 **4173**，任务 CLI 默认 **4176**；使用其他端口时需在 CLI 子命令前传相同的 `--port`。API 与 Worker 的自定义数据、项目、运行和副本目录必须一致。

```powershell
.venv/Scripts/python.exe -m repo_agent.cli.task_cli --port 4176 list
```

简化路径入口会自动登记本机目标，不要求预先手写项目配置。已有项目配置继续兼容。原 `repo_agent.task_http`、`repo_agent.task_cli` 和 `repo_agent.import_task_history` 命令保留为薄入口；直接执行 CLI `python -m repo_agent` 继续兼容，不经过网页队列。

开发时可给 API 和 Worker 加 `--reload`。它不会立即中断在途任务，适用于本机开发；详细行为见 [Worker 文档](docs/worker-pool.md)。

## 检查与结果如何理解

简化任务默认不运行测试。需要检查时，在高级设置中明确开启并提供检查目标；当前检查工具执行 pytest，不自动推断任意语言的构建或测试命令。

模型 `finish`、`completion.status`、检查通过及独立验收是不同结论。修改类任务无成功编辑时直接宣称完成会被反馈要求继续；“已有代码满足”“受阻”“未完成”须附说明。该规则改善交付记录，不保证需求一定实现。

离线示例与测试使用合成仓库或注入模型：

```powershell
.venv/Scripts/python.exe -m pip install pytest
.venv/Scripts/python.exe -m examples.offline_smoke
.venv/Scripts/python.exe -m pytest tests -q
node --test frontend/tests/task-ui.test.cjs
```

Node.js 仅用于前端检查，工作台静态页面无需 npm 构建。真实 PostgreSQL/Redis 集成测试需显式配置专用测试环境变量，见 [数据库测试](docs/database-tasks.md)和 [Worker 测试](docs/worker-pool.md)；跳过这些测试不等于真实集成通过。本轮检查范围见 [V0.4 状态报告](docs/v0.4/status.md)。

## 代码与材料

| 位置 | 职责 |
| --- | --- |
| `repo_agent/backend/` | HTTP、服务、任务仓储、契约与 Worker 入口 |
| `repo_agent/core/` | Agent 循环、Provider、工具、工作副本 |
| `repo_agent/experimentation/` | 固定版本准备、补丁导出与独立验收 |
| `repo_agent/infrastructure/` | 文件操作、锁、缓存、重载、路径解析和源码证据 |
| `repo_agent/cli/` | 任务客户端与显式历史导入 |
| `frontend/` | 本机工作台页面及前端检查 |
| `examples/fixtures/` | 受版本管理的冻结回归补丁 |
| `tests/` | 离线检查及可选外部服务集成测试 |

任务按“项目 → 需求 → 多轮运行”组织。新运行材料通常位于 `runs/<taskKey>/run_<ID>/`，Git 副本位于 `experiments/<project>/workspaces/`；旧目录兼容读取。数据库保存任务、调度和回收站状态，日志、Diff、直接修改基线等仍有文件材料。历史导入是显式操作，不会随正常启动迁移全部旧材料。

**`.env`、`runs/`、`experiments/` 和虚拟环境被 Git 忽略。** 上传源码与文档不等于备份数据库、工作副本、模型轨迹或完整实验材料；新克隆不自带本机历史。直接修改目标本身不因删除任务记录而回滚。

## 文档导航与下一阶段

- [V0.4 状态与收尾](docs/v0.4/status.md)、[后端结构](docs/backend-structure.md)
- [MVP 0.2 历史实验](docs/engineering-status-2026-10-29.md)、[V0.3 历史记录](docs/v0.3/README.md)
- [browser-use 实验手册](docs/experiment-01-runbook.md)、[pydantic-ai 实验手册](docs/experiment-02-runbook.md)

后续优先补充受控执行环境、任务停止/恢复策略、真实需求交付评测和调用成本测量。当前不宣称已支持安全沙箱、模型断点续跑、通用修复成功率、跨机器完整复现或多租户部署。