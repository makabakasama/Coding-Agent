# V0.5 第一个真实任务：任务状态更新接口（已冻结，待执行）

状态：**G / R 已冻结，G+R 基线已验证；真实模型执行尚未启动。** 本文与同目录的
`regression.patch` 一起受版本管理，是公开的权威需求与验收定义。本机运行材料、
工作副本和环境元数据保存在被 Git 忽略的 `experiments/backend-tasks/` 与
`runs/` 下，不随源码公开。

## 为什么选这个任务

目标是一个本机的 FastAPI 小型任务 API（约 416 行 Python）：`POST /tasks` 创建任务并推入
Redis 队列，`GET /tasks/{id}` 带 30 秒读取缓存，`GET /tasks` 列表，独立 worker 消费队列并
通过 `TaskRepository.update_status()` 把状态改为 `running` / `completed` / `failed`。

现状核对结果：

- `update_status()` 在仓储层已存在，但**服务层和路由层没有任何状态更新入口**。
- 读取缓存只在 `get_task` 中写入，没有任何失效路径；一旦状态可变，必然出现陈旧读。
- 需求涉及 3 个业务文件，含状态契约、校验、404 处理与缓存一致性，不是改文案。
- 目标项目与 Coding-Agent 自身无关，排错时不会把 Agent 的问题和目标项目的问题混在一起。

这是一个**未实现的新功能**，不是已知答案实验：结果由冻结测试判定，不看模型总结。

## 需求契约

```
PATCH /tasks/{task_id}
Header: X-API-Key（沿用路由级鉴权）
Body:   {"status": "pending" | "running" | "completed" | "failed"}
```

| 编号 | 预期行为 |
| --- | --- |
| 1 | 合法更新返回 200 与更新后的 `TaskRead`（id / goal / priority / status） |
| 2 | 四种之外的 status 在到达仓储前被拒绝，返回 422 |
| 3 | 不存在的 task_id 返回 404 与 `{"detail": "Task not found"}` |
| 4 | 成功更新后必须清除或刷新 `task:{id}` 缓存；随后 GET 返回**新**状态 |
| 5 | 缓存后端在失效时抛 `redis.exceptions.RedisError`，状态更新仍须成功（降级） |

`pending` / `running` / `completed` / `failed` 是本次冻结的接口契约：`running`、
`completed`、`failed` 与现有 worker 一致，`pending` 作为创建后的初始态。本任务不要求修改
数据库表、worker 或已有状态值。

**允许修改**：`demo_projects/app/schema.py`、`app/service.py`、`app/router.py`。
**禁止修改**：任何测试文件（含本回归补丁）、配置、`worker.py`、`app/main.py`、
`app/repository.py`、`app/dependencies.py`、`app/security.py`、`requirements.txt`。
`TaskService.__init__(repository, redis_client)` 签名与
`TaskRepository.update_status(task_id, status)` 契约保持不变。

## 冻结的 R

权威补丁：`examples/fixtures/backend-tasks-status-endpoint/regression.patch`

- SHA256：`662cc70b89d4241a4f283d9ce1d419837ebc1425605c2485a8db9a517358588a`
- 新增文件：`demo_projects/test/test_status_endpoint.py`（6 个测试）
- 目标：`test_patch_status_returns_updated_task`、
  `test_patch_status_rejects_unknown_status`、
  `test_patch_status_missing_task_returns_404`、
  `test_patch_status_invalidates_read_cache`、
  `test_patch_status_survives_cache_backend_failure`
- 内部控制：`test_existing_get_task_still_works`（已有 GET 不得回归）
- 外部控制：`demo_projects/test/test_health.py::test_health`

测试用 FastAPI 依赖注入 + 内存仓储 / 内存缓存驱动**真实的 `TaskService`**，不需要
PostgreSQL 或 Redis 服务，因此独立验收可在干净副本中重放。测试自身设置测试用 API key，
不读取任何真实凭据。

## 冻结的 G 及其偏差（如实记录）

原始目标目录**不是 Git 仓库**，因此这里没有「上游历史 commit」。冻结方式是：把目录复制到
`experiments/backend-tasks/inputs/source/`，逐文件记录 SHA256 作为来源证据，再创建**一个
本机合成基线 commit** `ffe361fc1d5742754df83320349d992febb19ea1`，让 worktree 与 G+R
重放有固定 revision。

已知偏差：原始文件是 CRLF，创建基线时全局 `core.autocrlf=true` 使 Git 把换行归一化为 LF，
因此冻结 commit 与原始目录是**内容等价而非字节相同**。G+R 重放以冻结 revision 为准，
哈希清单用于溯源。基线仓库已设 `core.autocrlf=false`，后续 checkout 保持提交字节。
原始项目全程只读，未写回任何内容。

## G+R 基线验证结果（2026-11-09）

在独立新副本恢复 G、应用 R 后运行专用测试环境：

| 检查 | 退出码 | 结果 |
| --- | --- | --- |
| R 全文件 | 1 | **5 failed / 1 passed**，失败原因均为 `405 Method Not Allowed`（端点不存在） |
| `test_health.py` 控制 | 0 | 1 passed |
| R 内部控制 | — | 1 passed（含在上表） |

`405` 是明确的功能缺失，不是环境、依赖或导入错误。原始输出、解释器与依赖版本保存在本机
需求目录，不随源码公开。

项目现状另有一项如实记录：目标项目原有的
`demo_projects/test/test_tasks.py::test_get_missing_task_returns_404` 在基线上**本来就失败**
（返回 401，因为现有路由级鉴权要求 `X-API-Key`，而该旧测试未携带）。它不属于本次需求，
也未被当作控制测试；本次不允许修改测试文件，因此保持原状。

## 执行与独立验收约定

执行入口为 `examples/backend_tasks_status_endpoint.py`（本仓库既有 G/R/P 流程，
`--run` 调用真实模型，`--prepare` / `--verify` 不调用模型）。流程为：固定 G+R → 验证
目标缺陷可复现 → Agent 在独立 worktree 中修改 → 单独导出 P 与 SHA256 → 在**另一个新副本**
恢复 G+R、应用 P、由验收流程运行预先指定的回归与控制测试 → 人工审查交付代码。

模型 `finish`、检查通过、独立验收通过是三种不同结论，不得互相替代。`--run` 会调用
外部模型并产生费用，须在明确授权后启动；本文不宣称已经执行过真实模型。
