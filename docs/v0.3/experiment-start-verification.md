# 固定实验需求新增轮次交付

日期：2026-11-02。本批实现 browser-use #5998、pydantic-ai #10525 的网页启动入口，契约见 [history-api.md](history-api.md)。

## 实际行为

- 任务页同时显示 `coding-agent` 普通任务项目和 browser-use、pydantic-ai 固定实验项目；固定实验项目进入原需求轮次页。
- 需求页先 GET 权威运行状态。用户只有点击“开始新一轮真实实验”、核对固定说明并再次点击“确认启动”后，才 POST 空 JSON。
- 后端复用原 `ExperimentRunner.run_experiment`；每轮生成新的北京时间 round ID、独立工作副本、G/R 准备、真实 Provider 调用、P 导出和独立 verify。
- 普通任务与实验共享 `runs/tasks/execution-global.lock`，同一时间只允许一个执行。
- 新轮次页面显示当前阶段、准备、Agent、P 导出及独立验收，并对 running/preparing 等原报告状态每 2.5 秒重读。

## 离线检查

```text
pytest tests/test_experiment_runs.py tests/test_task_list.py -q
5 passed

pytest tests/test_experiment_pipeline.py tests/test_second_experiment.py -q
23 passed, 2 subtests passed

node --check frontend/app.js
通过

node --test frontend/tests/task-ui.test.cjs
32 passed
```

新增运行测试使用注入 Runner，不调用真实 Provider、源码目标测试或模型。测试覆盖空请求、登记身份、重复启动锁、终态状态、HTTP POST/GET 与显式空 JSON 确认。

## 4176 落地核对

按原参数重启 `repo_agent.task_http --port 4176 --workspace-root experiments/tasks` 后，只读 GET 实际确认：

- `/api/projects`：browser-use 与 pydantic-ai 均为 `canStartExperiment:true`。
- 两个 `/runs/status`：`run:null, active:false, canStart:true, liveness:not_assessed`。
- browser-use 仍有 11 条历史导航记录；pydantic-ai 仍有 5 条。
- 原 coding-agent 普通任务记录仍存在。
- 4176 的 `app.js` 已包含固定实验项目卡片、显式启动与轮次状态自动刷新。
- `GET /runs` 返回 405；带非空对象的 POST 返回 400 VALIDATION_ERROR，均未调用模型。

本轮没有向 `/runs` 发送真实启动请求，因此未产生新的模型轨迹或独立验收。真实模型运行由用户在页面确认启动。

## 后续使用问题收敛（2026-11-02）

- 项目运行状态只返回属于当前项目和需求的轮次；其他项目执行时仍通过全局占用禁止启动，不生成错误的轮次链接。
- 状态读取失败后可以再次查询；启动响应丢失时暂停再次启动，先查询权威状态。查询本身不重发启动请求。
- 根 README 更新为当前网页执行与历史浏览能力，明确普通任务和固定实验的存储仍未全部统一、三类场景验收仍未完成。

本次实际检查：`pytest tests/test_experiment_runs.py -q` 为 **4 passed**；`node --test frontend/tests/task-ui.test.cjs` 为 **34 passed**；`node --check frontend/app.js` 和 `git diff --check` 通过。新增检查覆盖跨项目轮次归属、查询失败恢复和启动结果未知后只读确认。未运行全套回归、真实模型或浏览器联调；上述旧 4176 核对不代表本次修改已加载到运行中的服务。

## 用户启动的真实网页轮次

后续只读查询 4176 的运行控制和轮次接口，发现用户已启动并完成 pydantic-ai #10525 的 `20261102-153009_fdeb5c70`。运行控制记录的时间为北京时间 15:30:09 至 15:36:59，总报告为 `independently_verified`；轮次详情为 `stage:complete`、`baseline_verified`、`agent_reported_finished`、`candidate_exported`。`verify-01` 记录独立验收通过，5 个预定测试目标分别 1 passed、退出码 0、未超时。

候选 P 与验收报告记录的 SHA256 均为 `da5d7adfc29344fe4de02c8a68606ad1c91dbd47573a0728863dc4e9a258c72a`。本次只核对保存的报告，没有重新计算原件哈希或重跑验收；历史接口仍标记 `integrityStatus:not_checked`。browser-use 新网页入口的真实轮次验收未在本批核对。上述结果由用户运行产生，本批开发没有启动模型。
