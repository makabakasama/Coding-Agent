# Coding Agent Starter v0.1 — 实习求职保底项目

**目标：** 从最小的 Repo Coding Agent 开始，围绕真实 GitHub Issue 建立“观察代码 → 决策 → 修改 → 测试 → 反馈”的可重复实验闭环。

这不是完整的 SWE-agent/OpenHands，当前属于 **MVP 0.1**。已经用离线脚本和单元测试验证基础功能，**尚未用真实大模型自动解决 GitHub Issue**。

2026-10-20 本机增量开发：已补充真实模型 Smoke Test 和固定版本 Worktree 工具，
当前验收与 Qwen 启动方式见 [实验执行手册](docs/experiment-01-runbook.md)。
使用已有 Conda 环境 `coding-agent`，不是默认的 Conda base 环境。

## 目前具备的功能

- [x] 读取仓库文件列表、关键词检索、按行读取文件
- [x] 原生工具调用决策（list_files/search_code/read_file/replace_lines/run_tests/finish），由程序声明参数结构、包装内部决策与反馈
- [x] 快照行编辑：程序保存读取时的原始字节和 SHA256，编辑前核对整文件版本，再生成并应用 diff
- [x] 使用 `git apply --check` 验证与应用 unified diff
- [x] 使用 `python -m pytest` 执行定向测试（固定测试命令，禁止任意 Shell Tool）
- [x] OpenAI-compatible Chat Completions API 适配层（可以连接兼容服务）
- [x] 有最大决策步数、工具错误反馈、JSONL Trace
- [x] 写文件与运行测试需显式授权，路径检查防止跳出工作区
- [x] 无模型 API 的离线端到端测试
- [ ] 真实模型 + 真实 GitHub Issue 端到端验证
- [ ] 更强的上下文压缩 / AST 或符号级代码检索
- [ ] Docker 隔离执行、可复现评测矩阵、断点恢复

模型编辑已有文件时使用 `read_file` 返回的 `snapshot_id`，通过 `replace_lines`
指定已展示的行范围和新内容。文件版本变化或成功编辑后须重新读取；原有
`replace_text` / `apply_patch` 保留为本地兼容函数，不作为模型可调用的编辑入口。

## Windows 11 / PowerShell 运行

要求：Python >=3.10、Git。使用 `pytest` 跑待修复仓库的测试时还需 `pytest` 和该仓库的依赖。

```powershell
# 解压后进入源码根目录
cd coding_agent_starter
python -m pip install pytest

# 运行项目自身的单元测试
python -m unittest discover -s tests -v

# 离线模拟一次完整的 read -> patch -> test -> finish
python -m examples.offline_smoke
```

## 连接真实模型（可选）

项目根目录已提供本机 `.env`，分别填写 `QWEN_*` 和 `GPT_*` 两套模型、URL、密钥后，
`scripts/live-qwen-smoke.ps1` 和 Agent CLI 会自动读取，无需每次输入。
`.env.example` 可作为配置模板，`.env` 已排除 Git 跟踪。
配置优先级：显式参数 > 进程环境变量 > 项目 `.env`。
在 `.env` 中用 `LLM_PROFILE=qwen` 或 `LLM_PROFILE=gpt` 切换整套配置。
选定 profile 后只读取对应的 `QWEN_MODEL/BASE_URL/API_KEY` 或 `GPT_MODEL/BASE_URL/API_KEY`，缺项不会借用另一套或旧配置。
未设置 `LLM_PROFILE` 时保留旧行为：同一来源内优先读取 `LLM_*`，兼容 `OPENAI_*` 和 `DASHSCOPE_API_KEY`。
`LLM_PROVIDER` 支持 `openai_compatible`；完整示例见 `.env.example`。

在 PowerShell 中为兼容的 API 设置环境变量：

```powershell
$env:LLM_PROVIDER="openai_compatible"
$env:LLM_PROFILE="gpt" # 改为 qwen 即可使用另一套已保存配置
```

模型接口需支持 Chat Completions 原生 `tools` / `tool_calls`。模型选择工具与参数，程序构造 `Decision`、校验字段并序列化日志和工具反馈；不再解析普通回复中的决策 JSON。接口返回的参数仍需校验，格式错误会反馈给模型并消耗原有步数预算，不保证消除所有模型输出错误。本地兼容服务也需支持这一协议。没有 API 时先做离线测试即可。

**强烈建议在临时的、可丢弃的 Git clone 中运行。** 示例：

```powershell
# 先准备好一个独立的测试仓库（不是自己的工作主目录）
python -m repo_agent --repo "E:\\Projects\\Work\\test-repo" `
    --task "Investigate the reported bug, make the smallest patch, and run a focused test." `
    --max-steps 12 --allow-writes --allow-tests
```

注意：`--allow-tests` 执行仓库内 pytest，**测试代码本身可能运行任意 Python**；本程序还不是 Docker 沙箱，不要用来处理不可信代码或含有秘密的工作目录。不要将 API Key 写入仓库或上传 Trace。`--allow-writes` 将真的修改 clone。

如果只想审查分析过程，不给修改和执行权限，去掉 `--allow-writes --allow-tests`；模型会得到工具拒绝的提示。

## 项目结构

```text
repo_agent/
  agent.py        # 最小 Agent Loop + JSONL Trace
  provider.py     # 原生 Function Calling 接口
  decisions.py    # 程序定义的工具 Schema、Decision 和反馈包装
  prompts.py      # 工具使用与验证要求
  tools.py        # 仓库读/查/补丁/定向测试工具
  __main__.py     # CLI
examples/offline_smoke.py
  # 无模型、无网络、无第三方依赖的 Agent 操作模拟
tests/test_tools.py
  # 工具与离线循环测试
docs/repo-selection.md
  # 三个真实 GitHub 仓库的比较
docs/experiment-01.md
  # 首个外部 Issue 实验的步骤和验收标准
```

## v0.2 重点

1. 基于**已知答案、已有修复 PR 的历史版本**跑第一组 real-world issue regression；目的在于验证 Agent 与工具链，而非重复提交 PR。
2. 在实际 clone 中复现 Issue 的失败测试，记录 base commit / Python / 依赖版本 / failing test。
3. 确定另一个未被贡献者占用的 Issue，形成真实改进 PR 或自己的工具改进。
4. 开始比较 Agent 的基本版与改进版，而不是只展示一次成功运行。

## 相关资料

- [browser-use #5998](https://github.com/browser-use/browser-use/issues/5998)：keyword-only tool arguments。
- [browser-use #5999](https://github.com/browser-use/browser-use/pull/5999)：作者已有 PR。我们仅使用该 Issue 做已知答案实验。
- [browser-use #6031](https://github.com/browser-use/browser-use/issues/6031)：Agent 误报成功与服务器状态验证，后续可作为 benchmark 设计参考。
- [langgraph #9338](https://github.com/langchain-ai/langgraph/issues/9338)：`from __future__ import annotations` 下 ToolNode error handler 报错。
- [pydantic-ai #10525](https://github.com/pydantic/pydantic-ai/issues/10525)：`python -O` 下非对象 tool args；已有相关 PR，不作为立即申请的贡献机会。
