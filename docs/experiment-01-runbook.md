# Experiment 01：当前本机执行入口

## 目标与顺序

先验证真实模型能读取工具反馈，再让 Agent 在固定历史版本上尝试修复
browser-use #5998。保留首轮失败，不提前向 Agent 提供参考 PR 的实现。
Worktree 隔离文件修改，不是执行沙箱。

## 已核实的起点（2026-10-20）

- 当前目录原始代码是 v0.1，并非历史聊天中交付的 v0.2。
- 使用 Conda 环境 `coding-agent`，Python 3.11.16；原有 6 项测试和离线闭环通过。
- 本轮新增真实模型只读验证、固定版本 Worktree 创建、决策参数校验与测试字节码隔离。
- 本轮 10 项单元测试通过，离线闭环通过。
- 历史实验工作副本：`experiments/browser-use-5998`。
- base commit：`7be96ed8bafa8dfe1eef228b59cf5c884b8b2431`，本机已验证该提交存在并创建 detached Worktree。
- 真实模型、缺陷复现、Agent 修复与独立回归验收仍待验证；不以本机工具测试代替这些结果。
- 外部环境已创建，但依赖安装在下载构建依赖时长时间无进展，已中止；尚未安装完成。

## 1. 真实 Qwen 只读验证

项目根目录的 PowerShell 中运行：

```powershell
.\scripts\live-qwen-smoke.ps1
```

先在项目根目录的 `.env` 中填写 `DASHSCOPE_API_KEY`，保存后脚本会自动读取。
`.env.example` 是不含密钥的配置模板；`.env` 已在 `.gitignore` 中排除。
初始配置沿用标准百炼北京兼容接口及 `qwen3-coder-flash`。
以控制台实际开放的模型及地域为准，可显式覆盖：

```powershell
.\scripts\live-qwen-smoke.ps1 -Model '你的模型ID' -BaseUrl '你的兼容接口地址'
```

参考官方接口说明：
https://help.aliyun.com/zh/model-studio/other-tools-token-plan
https://help.aliyun.com/zh/model-studio/qwen-coder

Provider 统一读取项目根目录 `.env`，真实 Agent CLI 和连通性脚本都可复用。
配置优先级：显式参数 > 进程环境变量 > `.env`。密钥先检查进程中的两种变量，
再检查文件中的两种变量；同一来源中 `OPENAI_API_KEY` 优先于 `DASHSCOPE_API_KEY`。
脚本不再弹出密钥输入提示。标准 API 与套餐接口应使用各自匹配的地址及密钥。
`.env` 支持每行 `KEY=value`、整行注释及可选外层引号，不支持变量展开或行尾注释。

通过标准是模型先执行 `list_files`，再在 `finish` 中准确返回工具观察到的随机文件名。
验证只发送人工合成的临时文件信息，不发送用户仓库内容。最多 3 次模型请求，
轨迹位于 `runs/live-smoke/`。出现 `LIVE_LLM_SMOKE_PASS` 才算真实链路通过。
这不代表自动修复任务通过。

## 2. 工作副本与测试环境

已创建的工作副本可通过以下命令核对：

```powershell
git -C experiments/browser-use-5998 rev-parse HEAD
git -C experiments/browser-use-5998 status --short
```

需要额外新建实验副本时使用新目录，工具会拒绝覆盖已有目录：

```powershell
conda run -n coding-agent python -m repo_agent.workspace --source experiments/browser-use-source --destination experiments/browser-use-5998-run02 --revision 7be96ed8bafa8dfe1eef228b59cf5c884b8b2431
```

外部仓库使用独立环境 `experiments/browser-use-env`，避免依赖污染 Agent 环境。
本轮已尝试以下安装，网络恢复后可继续：

```powershell
.\experiments\browser-use-env\Scripts\python.exe -m pip install --timeout 15 --retries 1 -e .\experiments\browser-use-5998 pytest pytest-asyncio pytest-xdist pytest-timeout
```

安装对应历史源码及 pytest 插件后，先执行 Issue 原始复现，记录完整错误、
依赖版本与解释器。依赖导入错误不算缺陷复现。

## 3. 后续验收

1. 在原始工作副本运行独立回归用例，证明缺陷存在。
2. 给 Agent 提供任务描述和测试入口，不给参考补丁。
3. 保存 JSONL 轨迹、补丁、测试反馈和终止原因。
4. 独立重跑回归用例及相关现有测试，检查是否修改或弱化测试。
5. 完成首轮记录后再对照 PR #5999，分析定位、编辑、环境、验证各环节的问题。

首轮 Agent 调用使用 `--test-python experiments/browser-use-env/Scripts/python.exe`
将测试执行器指向外部仓库环境。该路径在启动时解析为绝对路径。
