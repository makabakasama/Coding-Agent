# 持久任务队列与独立 Worker

API 与 Worker 是两个独立进程，共用 PostgreSQL 和本机的任务、工作副本目录。Worker 进程通过线程池提供 3 个执行槽位，每个槽位同步运行一轮现有 Agent 流程。

## 启动

在 `.env` 中配置 `DATABASE_URL` 和模型配置，并完成项目登记。启动 API。

```powershell
.venv/Scripts/python.exe -m repo_agent.backend.app --port 4173
```

在另一个终端启动 Worker。

```powershell
.venv/Scripts/python.exe -m repo_agent.backend.worker --workers 3
```

两条命令均在项目根目录运行。自定义目录时，两个进程的 `--data-dir`、`--projects`、`--workspace-root`、`--runs-root` 和 `--project-workspaces` 应保持一致。API 和 Worker 启动均要求数据库配置。

Worker 终端输出包含时间、线程名、任务 ID、执行 ID、领取、执行步骤、工具结果和结束原因。开始执行时会打印项目、任务类型和固定 commit，便于确认实际读取的版本。日志只输出事件元数据，详细报告仍通过任务结果及事件材料查看。更新代码后需重启 Worker 以加载新的日志逻辑。

任务固定的是 Git commit，准备的工作副本来自该提交。主目录中的未提交改动不会进入任务工作副本。要分析新实现，应先提交代码，再建立新轮次并准备副本。

网页保存需求及轮次后，先准备独立工作副本，再点击“提交执行队列”并确认。该操作沿用 `POST /api/tasks/{id}/execution`，返回 `202` 和 `queued` 状态。Worker 关闭时，已入队任务继续保存在数据库中。启动 Worker 后会依次领取，最多同时执行 3 轮。

## 状态与并发

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
.venv/Scripts/python.exe -m pytest tests/test_task_queue.py -q
node --test frontend/tests/task-ui.test.cjs
```

这些检查证明调度及本地执行链路。真实模型完成真实需求仍需另行按项目实验约定验收。
