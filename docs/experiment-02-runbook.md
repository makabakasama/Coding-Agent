# Experiment 02：pydantic-ai #10525 历史缺陷实验

## 1. 目的与当前证据状态

检验 Coding Agent 能否跨到第二个仓库，在普通 Python 与 `python -O` 下恢复同一消息参数契约。
这是已知答案实验，不提交重复贡献。准备方仅用参考 PR 确认版本与现有契约，不把参考修复
diff 或实现方式放入 Agent 任务，也不修改上游原有测试来迁就参考答案。

截至本实现交付：已读取固定 G 的官方源码、原生测试、conftest、pyproject、uv.lock 和 CI；
准备入口、冻结 R 与框架离线检查已提供。**没有 clone/安装目标环境，没有执行真实 G+R、
真实模型修复或独立验收。全部真实阶段仍待用户执行。** 合成 oracle/临时 Git 单元测试
只验证实验代码的判定与路径隔离，不能冒充 pydantic-ai 缺陷已复现。

来源：

- [原始 Issue #10525](https://github.com/pydantic/pydantic-ai/issues/10525)
- [参考 PR #10491](https://github.com/pydantic/pydantic-ai/pull/10491)（准备方参考；不提供给 Agent）
- [固定 G](https://github.com/pydantic/pydantic-ai/tree/b4b9ad3de2d7a9cbfd7c70715f1f945496e990ce)
- [固定 G 的原生测试](https://github.com/pydantic/pydantic-ai/blob/b4b9ad3de2d7a9cbfd7c70715f1f945496e990ce/tests/test_messages.py)
- [固定 G 的 conftest](https://github.com/pydantic/pydantic-ai/blob/b4b9ad3de2d7a9cbfd7c70715f1f945496e990ce/tests/conftest.py)
- [固定 G 的 CI](https://github.com/pydantic/pydantic-ai/blob/b4b9ad3de2d7a9cbfd7c70715f1f945496e990ce/.github/workflows/ci.yml)
- [uv 命令说明](https://docs.astral.sh/uv/reference/cli/)

## 2. 固定 G、R 和允许的 P

- 源仓库：`https://github.com/pydantic/pydantic-ai.git`
- G：`b4b9ad3de2d7a9cbfd7c70715f1f945496e990ce`，参考修复提交的直接父提交。
- R：`examples/fixtures/pydantic_ai_10525/regression.patch`，只追加原生
  `tests/test_messages.py` 的回归/控制与必要的 `tempfile` 导入，不改任何既有测试。
- R SHA256：`4365ee7b3842d66dda5743dcae4d2e71282f5132455561a730be69c58ab28a20`。
- Agent 唯一可编辑业务文件：`pydantic_ai_slim/pydantic_ai/messages.py`。
- 测试、配置、其他业务文件均受保护；P 由既有导出/完整性审查流程单独保存。

普通模式原有 `raise_if_invalid=True` 对非对象 JSON 期望 `AssertionError`，本实验保留它。
不强加维护者尚未决定的异常类型变更。子进程回归约束默认返回 INVALID_JSON、非法输入
capability id 为 None，同时控制合法输入仍有效，不告诉 Agent 应如何修改实现。

固定目标：

| 角色 | `tests/test_messages.py::` 后的 node ID |
| --- | --- |
| 目标回归 | `test_issue_10525_non_object_json_optimized` |
| 普通子进程控制 | `test_issue_10525_non_object_json_normal` |
| 原生合法 JSON 控制 | `test_args_as_dict_valid_json` |
| 原生普通非对象控制 | `test_args_as_dict_non_dict_json_returns_invalid_json_wrapper` |
| 原生严格异常控制 | `test_args_as_dict_raise_if_invalid_non_dict_json` |

主 pytest 必须正常运行。R 内独立子进程分别 normal/`-O`，显式检查优化标志、解释器源码
路径及 pycache prefix；验收逻辑使用 `if`/显式失败，不依赖会被 `-O` 删除的 `assert`。
子进程显式传递父 pytest 的 `sys.pycache_prefix`，无父 prefix 时用新的临时缓存，避免
多次等长编辑命中过期 pyc。源码必须来自本工作副本内的 slim 和 graph 两个包。

目标行为不符才输出 `PYDANTIC_AI_10525_BEHAVIOR_MISMATCH` 并以专用返回码退出。
合法输入控制失败有独立的 `VALID_CONTROL_FAILURE` 标识；导入失败、来源错误、优化
标志错误、依赖/收集错误、超时都不能算目标复现。目标标识在子进程源码中分段构造，
避免 pytest 打印源码时仅凭标识文本误判。正常/优化子进程的来源信息会打印并进入完整日志。

## 3. 用户首次创建独立 source、工具环境和目标环境

本 Issue 现有 source、目标环境和工具环境均已迁至 [项目 inputs 布局](experiment-layout.md)。
当前机器无需重新安装；以下命令仅供首次创建时参考。

以下命令依据固定 G/原生锁文件拟定；本次迁移没有重新执行安装。网络、轮子
可用性、动态版本构建或平台依赖问题应保留为环境失败，不能声称 Bug 已复现。
不要为了让测试能运行而绕过原生 conftest、改 pyproject、换假包或安装未锁定的最新 SDK。

`tests/conftest.py` 会导入 `httpx/httpx2`、`cassetter`、`pytest_mock`、`dirty_equals`，
注册 cost guards、autouse fixtures 等。原生 dev 组还提供 anyio、inline-snapshot、
blockbuster 等；仅 `pip install pytest pydantic-ai-slim` 不足以证明原生测试可收集。
slim 的依赖包括同版本 graph、httpx2、griffelib 等，本实验使用该 G 的 `uv.lock`。

在项目根目录 PowerShell 执行。若任一路径已存在，先停止检查，不删除或覆盖现有材料；
安装中断后只续做确认尚未完成的步骤，不盲目整段重跑。已有 browser-use 环境完全不动。

```powershell
Set-Location E:\Projects\Work\Coding-Agent
$project = (Get-Location).Path
$sourceDir = Join-Path $project 'experiments\pydantic-ai\inputs\source'
$targetEnv = Join-Path $project 'experiments\pydantic-ai\inputs\env'
$toolsEnv = Join-Path $project 'experiments\pydantic-ai\inputs\tools-env'
$agentPython = Join-Path $project '.venv\Scripts\python.exe'
foreach ($path in @($sourceDir, $targetEnv, $toolsEnv)) {
    if (Test-Path -LiteralPath $path) { throw "已存在，先检查，不覆盖：$path" }
}
if (-not (Test-Path -LiteralPath $agentPython)) { throw '请先确认 Agent 项目解释器路径' }

git clone --no-checkout https://github.com/pydantic/pydantic-ai.git $sourceDir
if ($LASTEXITCODE -ne 0) { throw 'clone 失败，保留现场' }
$safeSource = $sourceDir.Replace('\', '/')
git -c "safe.directory=$safeSource" -C $sourceDir checkout --detach b4b9ad3de2d7a9cbfd7c70715f1f945496e990ce
if ($LASTEXITCODE -ne 0) { throw '固定 G 失败' }
git -c "safe.directory=$safeSource" -C $sourceDir rev-parse HEAD

# 工具也放新环境；不改 Agent 或 browser-use 环境。不要求 uv 在 PATH。
& $agentPython -m venv $toolsEnv
if ($LASTEXITCODE -ne 0) { throw '创建工具环境失败' }
$toolPython = Join-Path $toolsEnv 'Scripts\python.exe'
& $toolPython -m pip install 'uv>=0.9.25'
if ($LASTEXITCODE -ne 0) { throw 'uv 安装失败' }
& $toolPython -m uv --version

# 第一阶段只安装原生 dev 组；第二阶段保留 dev，加入该 G 的 slim/graph。
# --no-editable 防止安装路径绑定 source；实际测试导入由每个 worktree 的 PYTHONPATH 决定。
$priorUvEnv = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
$lockBefore = (Get-FileHash -LiteralPath (Join-Path $sourceDir 'uv.lock') -Algorithm SHA256).Hash
try {
    $env:UV_PROJECT_ENVIRONMENT = $targetEnv
    & $toolPython -m uv sync --project $sourceDir --locked --no-default-groups --only-group dev --no-editable --python $agentPython
    if ($LASTEXITCODE -ne 0) { throw '原生 dev 安装失败；不要修改锁文件兜底' }
    & $toolPython -m uv sync --project $sourceDir --locked --no-default-groups --package pydantic-ai-slim --no-editable --inexact --python $agentPython
    if ($LASTEXITCODE -ne 0) { throw 'slim/graph 安装失败；保留日志' }
} finally {
    if ($null -eq $priorUvEnv) { Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue }
    else { $env:UV_PROJECT_ENVIRONMENT = $priorUvEnv }
}
$lockAfter = (Get-FileHash -LiteralPath (Join-Path $sourceDir 'uv.lock') -Algorithm SHA256).Hash
if ($lockAfter -ne $lockBefore) { throw 'uv.lock 发生变化，停止，不能按冻结依赖宣称结果' }
& (Join-Path $targetEnv 'Scripts\python.exe') -c "import sys, pytest; print(sys.executable); print(pytest.__version__)"
```

两阶段命令沿用锁文件、上游 dev 组和 slim 包选择，不是已经验证过的最小依赖集。
`--inexact` 防止第二次 sync 移除第一阶段的测试依赖。安装如失败，将输出交主聊天分析，
不要关闭证书验证、忽略 conftest 或手工改成最新依赖。后续不使用 `uv run` 启动测试，
避免其隐式重新同步环境；框架直接调用目标解释器。不要把全局 `PYTHONPATH` 指向 source。

## 4. 用户检查真实 G+R（不调用模型）

```powershell
.\.venv\Scripts\python.exe -m examples.pydantic_ai_10525 --prepare
```

程序创建新的 `experiments/pydantic-ai/workspaces/10525_<ID>_agent`，记录放在 `runs/pydantic-ai-10525/prepare_<ID>/repair/`，恢复冻结 R，保存来源、完整输出、JUnit
与哈希。只在优化模式目标因指定行为失败、四个普通控制实际通过时才为 `baseline_verified`。
缺依赖、导入 source 或 site-packages、收集失败、跳过和其他错误都不是目标 Bug 复现。

Windows 下框架的 Git 操作使用每命令 `-c core.longpaths=true`，不修改持久 Git 配置或系统设置。
文件清单对超长路径使用 Windows 扩展 IO 路径读取，清单相对 key 和记录的工作副本路径保持不变。
若 Git 创建失败，新轮 manifest 的 `git_failure` 引用完整 `git-failure.stdout.txt`、
`git-failure.stderr.txt` 及其 SHA256，并记录命令、退出码和超时标志。失败轮保留，下一次创建新轮；
此前未保存的 Git 输出无法事后补造。这些处理不代表目标 pytest 的所有 Windows 平台兼容性均已验证。

用实际输出路径检查，不填历史浏览器实验清单：

```powershell
.\.venv\Scripts\python.exe -m examples.pydantic_ai_10525 --check-manifest "本轮 manifest.json 的绝对路径"
```

框架统一展开 `{workspace}`：prepare、启动检查、独立验收都将
`PYTHONPATH` 映射到各自新副本的 `pydantic_ai_slim` 和 `pydantic_graph`。
Agent 测试沿用该清单的解释器/环境，独立验收重新映射到另一个新副本，而非复用 Agent 副本。
正常 pytest 的导入探针和子进程中的精确来源校验共同防止 editable/site-packages 假复现。

## 5. 用户执行模型修复与独立验收

用户准备好并决定启动真实模型后，日常单命令：

```powershell
.\.venv\Scripts\python.exe -m examples.pydantic_ai_10525 --run
```

这会真实请求模型，并重新 prepare 全新副本；不会消费之前的检查轮。若要使用上一步
尚未认领的有效清单，可改用单阶段入口：

```powershell
.\.venv\Scripts\python.exe -m examples.pydantic_ai_10525 --manifest "本轮未认领 manifest.json 的绝对路径"
.\.venv\Scripts\python.exe -m examples.pydantic_ai_10525 --verify "对应 Agent 运行的 manifest.json 绝对路径"
```

`--run` 自动导出 P，按候选资格验收；Agent error/step_limit 与独立结果分开记录，
Ctrl+C 停止后续阶段。退出码 0 只表示这五个固定目标的独立重放通过，失败/跳过为 1，
用户中断为 130；`--manifest` 单阶段结束不等于独立验收完成。
全部准备/失败/运行/验收材料保留在 `runs/pydantic-ai-10525/`，总报告为
`run_<ID>/experiment.json`，同轮包含中文 `summary.md`、`repair/` 和 `verify-01/`。ID 为北京时间
`YYYYMMDD-HHMMSS_<8位ID>`；一次正式运行只分配一个 ID，副本为 `10525_<ID>_agent` 和
`10525_<ID>_verify-01`。重复 `--verify` 追加同轮 `verify-02` 等，不改原总报告；入口类型不会随状态改名。
历史成功轮次现位置：`runs/pydantic-ai-10525/run_20261029-144226_7c920922/summary.md`；
此位置整理沿用原始成功证据，不代表重新运行。所有轮次可从 `runs/pydantic-ai-10525/INDEX.md` 查找。
工作副本不代表执行沙箱，目录被 Git 忽略，不等于远端备份。

## 6. 复用边界与归档

公共 `ExperimentRunner` 只接收项目路径、任务、预算与必要依赖，不导入某个 example
去改其全局变量。5998 保留原入口和薄包装；第二入口有独立 Spec/TASK。
源码归档只额外允许 `examples/pydantic_ai_10525.py`，仍不复制 `.env` 或其他 example。
沿用源码证据、测试完整日志、P/R 分离、保护文件审查与独立新副本验收；不扩大 API 上下文、
不调整 HTTP 429/重试策略、不安装 Docker、不自动提交 PR。

准备代码与离线检查完成，不代表本机目标环境可用，更不代表第二个真实 Issue 已修复。
