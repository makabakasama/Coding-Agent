# 持久任务队列与独立 Worker

API 与 Worker 是两个独立进程，共用 PostgreSQL 和本机的任务、工作副本目录。Worker 进程通过线程池提供 3 个执行槽位，每个槽位同步运行一轮现有 Agent 流程。

## 启动

在 `.env` 中配置 `DATABASE_URL` 和模型配置；两个终端均先运行 `conda activate coding-agent`，再启动服务。简化路径入口会自动登记本机目标；也可使用已有项目配置。

```powershell
python -m repo_agent.backend.app --port 4173
```

在另一个终端启动 Worker。

```powershell
python -m repo_agent.backend.worker --workers 3
```

两条命令均在项目根目录运行。本文使用 API 默认端口 4173；任务 CLI 默认 4176，连接本例需显式指定 `--port 4173`。根 README 统一示例使用 4176。自定义目录时，两个进程的 `--data-dir`、`--projects`、`--workspace-root`、`--runs-root` 和 `--project-workspaces` 应保持一致。API 和 Worker 启动均要求数据库配置。

Worker 终端输出包含时间、线程名、任务 ID、执行 ID、领取、执行步骤、工具结果和结束原因。开始执行时会打印项目、任务类型和固定 commit，便于确认实际读取的版本。日志只输出事件元数据，详细报告仍通过任务结果及事件材料查看。更新代码后需重启 Worker 以加载新的日志逻辑。

开发时可使用自动重载入口，首次启用需用以下命令重新启动对应服务。

```powershell
python -m repo_agent.backend.app --port 4173 --reload
python -m repo_agent.backend.worker --workers 3 --reload
```

这两条命令分别在两个终端运行。监控范围包括 `repo_agent` 各已登记子包、登记的实验入口、项目配置及 `.env`。修改后 API 等待当前请求结束再重启，Worker 停止领取并等待已领取任务交付后重启。重启日志会显示等待与加载过程。HTML/CSS/JavaScript 由服务直接读取，修改这些文件后刷新浏览器即可加载。`--reload` 适用于本机开发，已领取任务较长时会等其结束后再加载代码。

选择 worktree 模式时，副本来自固定 Git commit，主目录未提交改动不会进入副本。direct 模式读取执行前的当前文件，包含未提交内容；普通目录无 Git commit，以文件基线记录来源。

网页保存需求及轮次后，隔离模式先准备独立工作副本，直接模式可直接点击“提交执行队列”并确认。直接模式的身份记录和执行前基线由 Worker 自动保存。该操作沿用 `POST /api/tasks/{id}/execution`，返回 `202` 和 `queued` 状态。Worker 关闭时，已入队任务继续保存在数据库中。启动 Worker 后会依次领取，最多同时执行 3 轮。

## 状态与并发

普通需求创建和新轮次页面可选择执行目录。

- `worktree` 使用固定 commit 创建隔离副本。完整契约省略模式时默认使用此模式，旧任务沿用；简化创建入口默认 direct。
- `direct` 使用登记目标的当前文件，支持 Git 和普通目录，包含未提交内容。Git 目标核对仓库身份与 HEAD，普通目录以文件基线核对；修改直接保存在目标中。相同目标根目录的直接任务串行领取，其他目标继续使用空闲槽位。

直接模式的执行前文件哈希和允许修改文件的原始内容保存在本轮 `execution/baseline/`，Diff 相对这份基线生成。失败后保留已产生修改，基线用于人工核对与恢复，系统不会自动回滚。执行期间应避免同时编辑相同文件。前端源码变动需要刷新页面，后端源码变动需要重启服务才会加载。

Git 目标的直接模式仍记录 commit，用于核对 HEAD；非 Git 目标的 baseCommit 为 null。源码内容以实际执行前文件基线为准。真实仓库修复实验继续使用原有独立副本和独立验收流程。

## 撤回直接修改

直接任务详情提供“检查能否撤回”和“撤回本轮修改”。确认后恢复本轮执行前的文件内容，恢复被删除的文件，并移除本轮新建文件。原执行报告、补丁、基线与日志保留，撤回状态单独记录在 `execution/undo-state.json`。

撤回需要本轮执行已终止、范围审查完整、执行前备份与执行后哈希记录可核对。执行后哈希保存在 `execution/undo-manifest.json`。已发生后续编辑的文件会作为冲突列出，此时整批撤回不会开始。同仓库仍有运行中领取记录时也会拒绝撤回。撤回期间应避免同时编辑目标文件。

撤回中途因文件占用等原因失败时会保留 `applying` 状态，重新检查后可继续。重复提交已完成的撤回返回原结果。旧轮次若缺少执行后哈希，页面会显示缺少安全撤回证据，需人工核对。

API 使用 `GET /api/tasks/{taskId}/execution/undo` 查询，使用 `POST` 并提交 `{"executionId":"本轮执行编号"}` 确认撤回。

直接模式允许测试日志位于当前仓库内已被 Git 忽略的目录，例如 `runs/.../execution/tests`。隔离模式继续要求日志位于工作副本外，未被忽略的仓库内日志目录会被拒绝。

`task_dispatch` 保存调度状态、固定执行编号、Worker 身份、领取时间、心跳和结束原因。`task_runs` 保留本轮任务契约。任务 ID 是调度表主键，重复提交返回同一轮状态。领取使用事务和 `FOR UPDATE SKIP LOCKED`，提交领取事务后才执行 Agent。

调度状态包括 `queued`、`running`、`terminated` 和 `unknown`。`terminated` 表示执行流程已终止，具体结果仍由 `stopReason`、实际检查和范围审查决定。页面继续展示 Diff、检查结果和未完成事项。

Worker 每秒更新已领取任务的心跳。连续 15 秒未更新时，查询接口显示待核实。心跳恢复后重新查询可确认当前状态。Worker 池重启时，旧的运行中领取记录转为 `unknown`，原因记为 `WORKER_INTERRUPTED`。已领取的轮次不自动重跑，确认需要再次执行时建立新轮次。

Worker 使用 PostgreSQL 会话锁限制同一任务目录只有一个池。每轮执行持有自己的文件锁，并保存原有工作副本和日志。Worker 池运行期间持有旧全局执行锁，登记的真实修复实验入口与整个 Worker 池互斥。开展该类实验前先停止 Worker 池。

按一次 Ctrl+C 会停止领取新任务，继续等待在途任务交付并维持心跳。强制结束进程后，已有修改及日志保留，后续查询和 Worker 重启按待核实规则处理。工作副本属于文件隔离，测试仍会执行本机仓库代码。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| `backend/repositories/task_queue.py` | 建表、幂等入队、领取、心跳、结果状态 |
| `backend/services/queued_execution.py` | API 的入队和执行状态查询 |
| `backend/services/worker_pool.py` | 3 个槽位的调度与执行 |
| `backend/services/task_execution.py` | `run_claimed` 同步执行及原有证据交付 |
| `backend/worker.py` | 独立进程启动 |

## 验证范围

`tests/test_task_queue.py` 在显式配置 `CODING_AGENT_TEST_DATABASE_URL` 时使用真实 PostgreSQL、独立 Worker 子进程、临时 Git 仓库及本地注入模型决策。覆盖重复点击、API 重建、3 槽并发上限、Worker 强制退出、材料保留、心跳失效及单池互斥。测试会清理本次随机需求的数据库记录。

```powershell
python -m pytest tests/test_task_queue.py -q
node --test frontend/tests/task-ui.test.cjs
```

这些检查证明调度及本地执行链路。真实模型完成真实需求仍需另行按项目实验约定验收。
