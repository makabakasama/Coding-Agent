# 统一任务命令行

V0.3 增量：命令行通过本地工作台 HTTP 接口操作同一套项目、需求与运行记录，不构造第二个 Agent 或存储。先启动工作台服务，默认端口 4176。原 `python -m repo_agent --repo ...` 保留为旧的直接执行入口；统一管理使用 `python -m repo_agent.task_cli`。

## 命令约定

```powershell
python -m repo_agent.task_cli projects
python -m repo_agent.task_cli list
python -m repo_agent.task_cli requirements coding-agent
python -m repo_agent.task_cli create --file task.json
python -m repo_agent.task_cli show <运行编号>
python -m repo_agent.task_cli organization <运行编号>
python -m repo_agent.task_cli rounds coding-agent <需求编号>
python -m repo_agent.task_cli new-round coding-agent <需求编号>
python -m repo_agent.task_cli prepare <运行编号>
python -m repo_agent.task_cli diagnostics <运行编号>
python -m repo_agent.task_cli status <运行编号>
python -m repo_agent.task_cli start <运行编号>
python -m repo_agent.task_cli events <运行编号> --after 0 --limit 50
python -m repo_agent.task_cli result <运行编号>
```

`create`、`new-round` 只保存；`prepare` 只准备副本；**`start` 会主动调用已配置模型**，返回后台接受状态，不等待模型完成。通过 status/events/result 查询；不会因查询而启动或重试。所有命令输出 JSON，成功退出 0；后端拒绝或连接失败退出 1，输入文件问题退出 2。可在子命令前用 `--port 4177` 指定另一个本地服务，只连接 127.0.0.1。

## 阅读需求输入示例

在本机创建 UTF-8 的 task.json：

```json
{
  "projectId": "coding-agent",
  "revision": "HEAD",
  "kind": "read_code",
  "current": "网页提交的任务如何进入 Agent 执行？",
  "expected": "说明调用顺序，标出相关文件和函数，以及尚未理解的部分。",
  "allowedPaths": [],
  "allowTests": false,
  "checkTargets": [],
  "maxSteps": 12
}
```

字段采用现有 API 契约；阅读的 current/expected 表示问题与期望分析，功能任务表示新增需求与验收条件，修复任务表示缺陷与预期行为。修复和功能须指定允许修改路径。源仓库未提交内容不会进入本轮，准确 commit 由服务解析。

同一需求再次执行用 new-round，再准备和启动。网页能找到命令行创建的轮次，命令行也能查询网页创建的记录。旧记录继续使用原运行编号；不迁移历史数据。

连接中断时写请求可能已经被接收：输出 COMMAND_OUTCOME_UNKNOWN，不自动重发。启动可通过 status 核对，准备通过 diagnostics 和网页准备卡片核对；创建通过 list/rounds 核对。不要把超时当作“没有执行”。

固定实验仍使用原 CLI 或已接通的网页启动，保留 G/R/P 语义；本命令行的 start 是普通任务执行接口，不把普通检查称为实验独立验收。

## 本批验证

`pytest tests/test_task_cli.py -q`：3 passed。使用临时合成仓库、真实本地 HTTP 和注入脚本模型，验证 CLI 创建后后端可读、同需求新增轮次、准备、启动、事件与结果，以及后端错误、坏输入文件和响应丢失不重发。先前 4 项输出包含意外导入收集的既有测试类，已修正，当前专项为 3 项。

实际对 4176 执行 help、projects 和 list，能读取已有网页项目与任务。本批未向真实服务发送创建、准备或启动请求，未安装依赖、未迁移材料、未提交推送。Windows 命令入口输出 UTF-8 JSON。
