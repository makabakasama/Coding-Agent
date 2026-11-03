# 普通需求与多轮运行

2026-11-02 增量。采用现有 `runs/<taskKey>/`、`experiments/<project>/workspaces/` 布局，不移动旧材料。

新建普通任务 POST `/api/tasks` 同时创建一个需求和首轮快照。返回的 32 位 taskId 继续作为运行身份，原准备、执行、结果接口保持不变。运行目录为 `runs/<project>-<需求ID>/run_<北京时间ID>/`，其中 task.json 是本轮冻结约定，round.json 关联项目、需求、轮次与运行编号；需求级 task.json 保存原约定。工作副本为 `experiments/<project>/workspaces/<taskKey>_<roundId>_agent`。

- GET `/api/projects/{project}/tasks`：普通项目返回普通需求列表，包含 taskKey、mode、title、roundCount。
- GET `/api/projects/{project}/tasks/{taskKey}`：需求详情，包含原约定 contract。
- GET `/api/projects/{project}/tasks/{taskKey}/rounds`：轮次列表，每项 roundId、runTaskId、createdAt、baseCommit、kind。通过 runTaskId 访问原执行和结果页。
- POST `/api/projects/{project}/tasks/{taskKey}/rounds`，请求 `{}`：按该需求原约定解析当前 requestedRevision，重新冻结 commit，创建新轮快照，返回 201 普通任务记录；不准备副本或调用模型。
- GET `/api/tasks/{runTaskId}/organization`：项目、需求与轮次关联，旧记录返回 null。

保存新轮次不会覆盖上一轮；新轮次开始后，需求原约定与已有快照均不修改。首版不提供需求编辑，变更需求应新建。普通任务仍无独立 G/R/P 验收，不能因目录统一改写结果含义。

旧 `runs/tasks/task_<ID>` 记录按原位置读取，保留原编号和工作副本身份，不复制或迁移；普通任务记录列表包含新旧轮次。旧记录未明确关联到统一需求时保持原任务入口，不猜测归属。

两个固定实验保留现有需求 ID 和 Spec/Runner，不接受普通任务的新轮次接口。它们继续通过 POST `/runs` 执行完整 G/R/P 实验，普通任务的 POST `/rounds` 只分配新一轮，调用模型仍需在执行页单独确认。

## 实际验证

`pytest tests/test_project_task_store.py tests/test_tasks.py tests/test_task_list.py -q`：19 passed、55 subtests passed。合成仓库验证同需求两轮、不同 commit、不同副本、脚本模型执行和结果、重建服务读取、新旧混合列表，以及旧 task.json 和首轮快照字节保持不变。前端 DOM 模拟共 36 passed，包含需求轮次展示及新增轮次只保存不调用执行接口。未启动真实模型，未移动本机旧材料，尚不以此代替实际浏览器操作验收。

服务入口默认使用 ProjectTaskStore；底层旧 TaskStore 保留兼容与既有调用。项目注册仍沿用现有 projects.json，runs/tasks 还存配置、全局锁和旧记录，不再作为默认新普通运行的结果目录。可用 `--runs-root`、`--project-workspaces` 显式隔离新记录与副本，旧 `--data-dir`、`--workspace-root` 负责旧记录及旧副本兼容。

补充兼容检查 `pytest tests/test_task_preparation.py tests/test_experiment_runs.py -q` 为 14 passed。4176 已加载新服务：普通项目需求列表可查询，旧真实普通任务仍为 terminated 且可读取，静态脚本包含新增轮次入口。当前尚无本机新普通需求，项目列表返回空需求是实际记录状态，不自动把旧单次任务改写成新需求；旧记录仍可通过“普通任务记录”找到。没有为演示向本机真实项目创建测试需求。
