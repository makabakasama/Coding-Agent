# Coding Agent MVP 0.2

一个面向学习和工程验证的仓库修复 Agent：连接真实 LLM，在独立 Git 工作副本中观察代码、执行受限编辑、运行定向测试，并用新的工作副本验收保存的补丁。

目前已在本机完成 **browser-use #5998** 和 **pydantic-ai #10525** 两项已知答案实验的独立验收。它们验证了两个具体历史缺陷上的修复链路，不代表通用修复成功率、模型稳定重复性或执行沙箱已经验证。两项均有公开参考修复，仅用于已知答案实验，不宣称参考修复已合入上游或由本项目贡献。

详细记录见 [2026-10-29 工程现状](docs/engineering-status-2026-10-29.md)。

## 当前系统

- **模型决策**：使用 OpenAI-compatible Chat Completions 原生 `tools` / `tool_calls`，校验工具名、参数和调用数量；不执行普通回复中的 JSON。
- **仓库操作**：在指定工作副本中列文件、搜索、读取和编辑。`read_file` 返回快照；`replace_lines` 核对原文件 SHA256 后生成并检查补丁，避免覆盖读取后发生的改动。
- **测试反馈**：工具运行固定形式的 pytest 命令，把结果反馈给模型；失败后可继续修复，受步数预算限制。
- **独立验收**：固定历史版本 G、冻结回归补丁 R；导出仅包含允许业务文件的 P，在另一个全新 G+R 副本中重放并测试。模型 `finish` 不算独立成功。
- **运行证据**：保留轨迹、候选补丁、文件哈希、测试完整输出、环境元数据和终止原因；新入口还保存允许范围内的 Agent 磁盘源码快照及测试日志。
- **工程兼容**：每命令 Git 长路径支持、Windows 长文件清单读取、统一轮次 ID、中文摘要与历史路径映射。

| 已知答案实验 | 代表轮模型 | 修复前 G+R | 新副本 G+R+P |
| --- | --- | --- | --- |
| browser-use #5998：keyword-only action 参数 | `gpt-6-luna`，13 步 | 目标失败，1 项控制通过 | 2 项固定目标均通过 |
| pydantic-ai #10525：`python -O` 下非对象 JSON 参数 | `gpt-6-luna`，12 步 | 优化模式目标失败，4 项控制通过 | 5 项固定目标均通过 |

#10525 本机另有一轮独立通过记录；这些选择性实验不构成成功率统计。browser-use 的旧轮没有后续新增的 Agent 源码归档，不事后补造该证据。

## 先运行离线示例

要求：Agent 使用 Python **3.11+**、Git；目标仓库使用各自的依赖环境。以下命令在项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m examples.offline_smoke
```

离线示例使用脚本模型和临时合成仓库，不调用真实 API。项目自身测试与目标仓库的缺陷复现是两类证据。

## 配置真实模型

本仓库提供 [.env.example](.env.example)，不提供密钥或可直接使用的本机 `.env`。

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

手动填写对应配置：

- `LLM_PROVIDER=openai_compatible`。
- `LLM_PROFILE=gpt` 或 `qwen`，选择对应的 `GPT_MODEL/BASE_URL/API_KEY` 或 `QWEN_MODEL/BASE_URL/API_KEY`。
- profile 是配置分组名，具体模型由 `*_MODEL` 决定。选中分组后，缺项不会借用另一分组。
- 配置优先级：显式构造参数、进程环境变量、项目 `.env`。未选择 profile 时兼容 `LLM_*` 等旧配置。

服务需要支持原生工具调用。传输层仅对部分 TLS EOF、超时和连接中断进行最多两次额外重试；HTTP 错误（包括 429）、证书校验失败和无效响应不会自动重试。请求重试不会重新执行本地工具。

真实实验需先准备目标源码、冻结 R 和独立环境，再按手册运行；这些命令会调用模型或执行仓库测试：

- [browser-use 实验手册](docs/experiment-01-runbook.md)
- [pydantic-ai 实验手册](docs/experiment-02-runbook.md)

Git worktree 只隔离文件。`--allow-tests` 或实验入口会执行仓库中的 Python，当前没有 Docker 执行沙箱。

## 代码与记录

| 位置 | 职责 |
| --- | --- |
| `repo_agent/agent.py`、`provider.py`、`decisions.py` | 决策循环、模型请求、工具协议与校验 |
| `repo_agent/tools.py`、`workspace.py` | 仓库工具、快照编辑、定向测试、独立工作副本 |
| `repo_agent/experiments.py`、`experiment_runner.py` | 固定 G+R 准备、认领与完整流程编排 |
| `repo_agent/experiment_artifacts.py`、`experiment_verify.py` | 导出 P、完整性检查、新副本独立验收 |
| `repo_agent/run_evidence.py` | Agent 磁盘源码证据与源码漂移检查 |
| `repo_agent/round_layout.py`、`path_migrations.py` | 统一命名、中文摘要、旧路径解析 |
| `examples/fixtures/` | 受版本管理的权威回归补丁 R |
| `tests/` | 合成仓库、模拟模型及框架行为的离线测试 |

本机记录按轮次保存：

```text
runs/<experiment>/run_<北京时间ID>/
  summary.md
  experiment.json
  repair/
  verify-01/
experiments/<project>/workspaces/
  <issue>_<ID>_agent/
  <issue>_<ID>_verify-01/
```

单独准备使用 `prepare_<ID>`；重复验收追加 `verify-02` 等。各 Issue 的本机 `INDEX.md` 用于导航，具体命名和迁移规则见 [目录说明](docs/experiment-layout.md)。

**`runs/`、`experiments/` 和 `.env` 被 Git 忽略。** 本次文档记录了本机实验结论及 G/R/P 校验信息，上传源码和文档不等于上传完整原始日志、模型轨迹、环境或工作副本。新克隆不会自带这些材料。

## 下一步

- Docker 沙箱与执行资源限制。
- API 调用、上下文和工具反馈的效率测量。
- 更多 Issue、失败案例和多次运行评测，形成明确分母与可比较指标。
- 依赖环境复建、跨机器补丁重放、上下文压缩与更精确的代码检索。
