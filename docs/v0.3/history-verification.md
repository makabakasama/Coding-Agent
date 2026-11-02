# 首批只读历史接入交付

日期：2026-11-02。本批已实现，接口字段见 [history-api.md](history-api.md)。仅接入现有两项目历史，统一写入/配置迁移及网页实验启动仍未实施。

## 已实现与来源

task_history.py 对已登记的 browser-use-5998、pydantic-ai-10525 读取 round.json、原 experiment/prepare/Agent/candidate 报告及 verify-* 报告。使用现有 Spec 工厂识别登记来源、round_layout 的 ID 格式和 recorded_path 解析已有迁移引用；不调用 Runner、prepare、verify、write_summary，不从当前 Spec 补造历史 G/R/需求快照。

项目接口增加 modes/historyAvailability/canCreateTask/readOnly；原普通任务接口和写入逻辑保持。历史新路由全部 GET，旧格式验收不升级为现代独立通过。6条未关联材料保留 taskKey=null 和 warnings，不猜归属。报告/日志/补丁只按登记材料ID脱敏分页读取，没有任意路径读取。

## 后端直接读取验证

先以自管临时端口做真实 HTTP 读取，随后在核实4176为本项目原服务、所有普通任务已终止且持有全局执行锁后，用原参数 `-m repo_agent.task_http --port 4176 --workspace-root experiments/tasks`、原数据根 runs/tasks 重启。新启动器PID为114156；未触碰其他端口。

4176 实际 GET 验证结果：

| 项目/需求 | 实际结果 |
| --- | --- |
| coding-agent | 原普通任务785d4df18fb74b3db558cb17fb63c026及结果可读取，最终检查原记录passed |
| browser-use / #5998 | 11条历史导航记录，其中6条未关联；读取样本run_20261026-175230_22903996的准备、Agent、候选导出和verify-01报告均200 |
| pydantic-ai / #10525 | 5条轮次；读取样本run_20261029-152335_25271a9c的准备、Agent、候选导出和verify-01报告均200 |
| 全局执行锁 | 重启后可获取，辅助过程未留下占用 |

实际核对 `/api/projects`、两个项目的需求/轮次/详情/验收/材料接口，以及原 `/api/tasks` 和已有真实结果。材料redacted=true。证据：`runs/engineering/history_readonly_20261102_0b0ced97/{restart.json,read-check.json,server.stdout.txt,server.stderr.txt}`。没有运行模型、目标测试/实验或完整回归，没有新建工作副本、重写原报告/摘要或迁移目录。

另外实际读取 browser-use prepare_20261024-153517_03354382 的旧 manifest 验收报告，材料200且 independentlyVerified 保持null，没有升级为现代验收通过。

此前 /api/projects 的旧单元断言只允许普通项目，现改为比较可创建普通任务的项目，适应新增只读历史项。仅运行 `pytest tests/test_tasks.py::TaskIntegrationTest::test_http_create_read_static_and_restart -q`，**1 passed，1.12秒**，检查既有 HTTP 保存/读取兼容；未扩展边界矩阵或运行全套回归。

## 使用入口与边界

- [项目历史](http://127.0.0.1:4176/#projects)
- [browser-use #5998](http://127.0.0.1:4176/#projects/browser-use/tasks/browser-use-5998)
- [pydantic-ai #10525](http://127.0.0.1:4176/#projects/pydantic-ai/tasks/pydantic-ai-10525)
- [原普通任务记录](http://127.0.0.1:4176/#tasks)

前端页面由前端会话维护；本后端完成接口与实际4176读取验证，没有自行进行浏览器验收。原报告中的独立验收通过只按原记录展示，integrityStatus=not_checked，本次没有重放P或重新验收。当前仍无网页新实验启动、配置迁移、全历史导入、未关联材料自动归属或统一多轮写入。没有提交或推送。
