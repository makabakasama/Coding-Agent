# M2a 后端运行说明

先阅读 [准备契约](preparation-api.md)。M1 保存查询使用方式继续见 [原运行说明](runbook.md)；其中没有副本功能的描述是 M1 当时的边界。

```powershell
.\.venv\Scripts\python.exe -m repo_agent.task_http --port 4176 --workspace-root experiments/tasks
```

使用未占用的端口，不结束未知服务。旧进程不会热加载新代码；现存 4175 服务是否切换由协调会话处理。本轮后端验证使用临时端口，没有接管该进程。

前端会话交付报告使用另一个独立临时合成环境，在 4177 完成 M2a 浏览器联调；报告交付时该服务仍运行，入口为 [临时合成任务详情](http://127.0.0.1:4177/#/tasks/29dca745b1ae4738b629cb7d8bc780d3)。这不是默认任务目录，也不表示旧 4175 已升级。后端收尾未重新探测服务，不应把此临时地址当作长期运行承诺；不要为复查而准备新的真实任务或结束未知服务。

保存任务后，前端单独点击准备时向 `/api/tasks/<ID>/preparation` POST `{}`；GET 同一地址恢复状态。POST 是同步操作，准备期间可 GET；断连后查询同一 ID，重复 POST 也不会创建第二份。失败没有自动重试或清理按钮。用户需要另建任务开展新一轮准备。

公共响应只显示 workspaceId 和准确 commit。本机管理员可在 `runs/tasks/task_<ID>/preparation/identity.json` 核对 source、workspace、head、gitDir、commonDir；失败时可能仅有准备前保存的预期路径。不能用该文件的预期路径推导创建已经成功。不要把这些本机路径复制到公开文档。

默认副本位于 `experiments/tasks/<projectId>/workspaces/`。准备不会安装依赖、运行模型或目标测试。worktree 不是沙箱；现阶段也没有执行、取消、修复结果或验收接口。不要手动删除失败声明或副本以强迫重试。

专项检查：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tasks.py tests/test_task_preparation.py tests/test_workspace.py -q
```

离线检查仅在临时合成仓库创建 worktree，不代表真实仓库 G/R/P 验收。检查报告见 [M2a 验收记录](m2a-verification.md)，其中分开记录后端直接执行证据与前端会话报告。本后端批次没有修改 frontend/。

前端已报告：刷新或点击“查询最新状态”通过 GET 恢复；POST 结果未知时不自动重发；失败禁用准备，不提供重试、清理或取消；Agent 启动始终禁用。浏览器已验证成功准备、刷新/查询恢复和人工中断声明夹具，HTTP 已验证重复 POST 同一编号及单副本、固定 HEAD。准备中、网络、损坏、旧响应仅模拟覆盖；跨标签页、服务重启未做浏览器验证。收尾只审阅代码并整合报告，没有重跑这些检查或完整回归。
