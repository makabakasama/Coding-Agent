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

## 4. 原生工具调用接口验证

当前决策使用 Chat Completions 的 `tools` / `tool_calls`。工具名称和参数 Schema
由程序声明，模型选择函数并填写参数；Provider 将结果校验为 `Decision` 对象，
Agent 使用 Python 字典和 `json.dumps()` 包装日志与 `role=tool` 反馈。
普通回复中的 action/args JSON 不再作为可执行决策解析。

接口参数不是无条件可信：缺失字段、错误类型、无效参数 JSON、读取范围越界，
以及多工具调用都会被拒绝并反馈，重试计入同一个步数预算。当前不假定服务支持
严格解码，也不自动切换回旧文本协议或更换模型。

更换该协议后，先在项目根目录运行只读连通性验证，复用当前 `.env` 模型配置：

```powershell
conda run --no-capture-output -n coding-agent python -m examples.live_llm_smoke
```

通过后再启动 #5998 实验。任务仍使用 20 步预算，测试依旧由模型选择调用。
本地模拟接口测试通过不代表当前账户的真实模型接口已经验证。

协议参考：https://help.aliyun.com/en/model-studio/qwen-function-calling

## 5. 通过清单启动本轮实验

先通过 `--prepare` 生成本轮清单。只检查清单与副本，不调用模型：

```powershell
conda run --no-capture-output -n coding-agent python -m examples.browser_use_5998 --check-manifest "本轮 manifest.json 的路径"
```

用户明确授权运行真实模型后，执行：

```powershell
conda run --no-capture-output -n coding-agent python -m examples.browser_use_5998 --manifest "本轮 manifest.json 的路径"
```

启动前必须确认准备成功、实验配置匹配、HEAD 未变、暂存区为空，冻结的 R 和文件清单
等证据哈希一致，工作副本中的已跟踪及未被忽略的文件与 G+R 清单完全一致。
旧的 `--repo` 选项已从这个实验入口移除；通用 `repo_agent` CLI 不属于此受控实验入口。

通过检查后，入口以独占创建 `agent-run.json` 的方式认领本轮；已有记录则拒绝复用。
即使启动异常或模型未修改代码，也应重新 prepare，保留原轮现场。准备清单保持原样，
运行状态记录在 `agent-run.json`，轨迹存入同轮 `agent/` 目录。模型配置继续来自现有
环境和 `.env`；测试解释器、超时和显式环境设置沿用准备清单。`step_limit` 保持原状态，
不会自动变成修复成功。运行终止后自动导出候选 P 和审查报告，独立验收需另行显式调用 `--verify`。

该检查不重跑测试、不重新导入目标库，也不检查已安装依赖的内容变化；不能防止
同时恶意修改清单与哈希。文件检查不覆盖被忽略的文件，不提供持续的跨进程文件锁。
一次性认领只防止同一份清单被两个实验入口重复使用。

## 6. 快照与真实文件一致性

`read_file` 从同一次字节读取生成展示内容、整文件 SHA256 和会话内的
`snapshot_id`，并返回实际展示的 `start` / `end`。字符上限只能截断在完整行边界；
模型不能修改未展示的行。

模型使用 `replace_lines(snapshot_id, start, end, new_text)` 提出完整行替换。
文件路径和旧内容由程序从快照获取，模型不再复制多行 `old_text`。
程序直接比较当前文件与快照的完整字节，连所选范围之外的变化也会导致拒绝。
生成 diff 后，以及 Git 检查和实际应用前，程序会再次核对版本。

编辑成功后，同文件的旧快照全部失效；下一次编辑必须重新读取。日志包含生成的
补丁、编辑前后 SHA256 和 `consistent`。如果应用后的实际文件与预期不同，
返回 `applied=true, consistent=false` 并提示重新读取，不自动回滚或覆盖外部修改。

该机制使用版本检查与应用后校验，不提供跨进程文件锁。模型仍须正确生成新代码
的缩进。快照只在当前工具会话中有效，进程重启后不能复用旧 ID。
原有替换和原始补丁函数只保留本地兼容调用，真实模型不能绕过快照编辑接口。

## 7. 自动准备本轮 G + R

在项目根目录运行：

```powershell
conda run --no-capture-output -n coding-agent python -m examples.browser_use_5998 --prepare
```

此模式不初始化模型、不请求 API。它检查冻结回归补丁的 SHA256，在固定历史提交上
创建新的 worktree，恢复回归测试，核对实际导入位置，并分别执行目标回归和普通参数
测试。目标用例必须因指定错误失败，控制测试必须实际通过；导入失败、收集错误、
超时或全部跳过都不会产生有效基线。

仅当测试前后的文件清单及内容哈希一致时，状态才为 `baseline_verified`，进程返回 0。
输出的 `manifest` 可传给 `--check-manifest` 或 `--manifest`；这两种模式与 `--prepare`
互斥。不要手动将旧副本路径替换进清单。

每轮材料位于 `runs/browser-use-5998/<run_id>/`，包括准备清单、冻结的 R、G+R 差异、
文件哈希、环境依赖、导入探针、完整测试 stdout/stderr 和 JUnit 结果。准备失败返回 1，
保留失败阶段、原因、已有日志及副本；不自动删除或重置。新 worktree 位于
`experiments/browser-use-5998-<run_id>`，旧实验副本保持原状。

prepare 只自动化准备与基线验证，运行入口另行保存认领状态和 Agent 轨迹；
完整环境与 Agent 源码归档仍待集成；独立补丁验收见第 9 节。

## 8. 自动导出候选 P 与运行结果

#5998 当前明确允许修改 `browser_use/tools/registry/service.py` 这一既有业务文件；
其他文件均属于本轮保护范围。这是本轮实验的补丁验收策略，不是工具层的写入沙箱。
新增文件需要后续明确扩展策略，当前会被视为非预期改动。

启动检查确认 R 没有修改允许编辑的业务文件，因此可以单独对这些文件生成相对 G 的
diff，得到从 G+R 到 G+R+P 的业务补丁。程序不按文件名猜测哪些测试变化属于 R。
整个工作副本相对 G 的 diff 包含 R，只用作审查证据，不能当作 P 应用。

正常 finish、达到步数上限或出现异常后，入口均尝试保存：

- `agent-run.json`：原始运行状态、返回结果、异常原因、模型非敏感信息及材料入口。
- `artifacts/agent.patch`：业务代码候选 P；报告保存其 SHA256，空补丁明确标为无代码改动。
- `artifacts/workspace-vs-g.patch`：包含 R 的全量 Git 差异，仅供审查；不包含未跟踪文件内容。
- `artifacts/final-files.json`：已跟踪及未忽略文件的最终内容哈希，补充记录新增、删除等变化。
- `artifacts/report.json`：保护文件审查、编辑哈希链、测试调用结果与导出状态。

报告同时检查最终文件和工具轨迹：即使保护文件改过后恢复，轨迹中成功应用的编辑仍
会被标记。业务文件必须与工具记录的 before/after 哈希链对应；导出期间再次改变文件、
HEAD 或暂存区，也不能自动进入独立验收。未跟踪文件与其他非法改动保留在原副本中，
不自动清理或回滚；最终审查只覆盖文件清单与已记录的工具操作，不提供操作系统隔离。

`candidate_exported` / `eligible_for_verification=true` 仅表示候选补丁完成本轮审查，
不表示测试通过或 Issue 修复成功；运行状态仍可为 `step_limit` 或错误。保护范围违规、
轨迹缺失、导出失败等情况会保留已有材料并禁止自动进入独立验收；状态分别记录，
不会用导出错误覆盖原始 Agent 失败。独立验收通过 `--verify` 自动另建干净副本。

Agent 的测试反馈仍沿用工具当前的输出截断；完整输出保存尚未接入 Agent 测试工具。
prepare 的完整 stdout/stderr 与 JUnit 保存不受此限制。

## 9. 独立重放验收已保存的 P

对已运行并完成候选导出的本轮准备清单执行：

```powershell
conda run --no-capture-output -n coding-agent python -m examples.browser_use_5998 --verify "原运行 manifest.json 的路径"
```

`--verify` 与其他入口模式互斥，不初始化 Provider 或 Agent，不请求模型。它读取原清单、
`agent-run.json`、`artifacts/report.json`、冻结的 R、P、文件指纹和轨迹，检查身份、
终止状态、实验配置、导出资格、保护范围及哈希。空 P、损坏或不合格材料会被拒绝，
不会从原工作副本重新生成补丁。原副本即使后来改变，也不能改变保存的候选输入。

每次调用在 `runs/browser-use-5998/verify_<时间>_<唯一ID>/` 新建证据目录，
主报告为 `verification.json`。输入副本放在 `inputs/`；复用 prepare 创建的全新 G+R
副本及基线证据位于本次 `baseline/` 下，报告记录准确的工作副本和清单位置。
重复验收同一候选也会新建副本与目录，不覆盖、认领或修改原运行记录。

固定验收顺序：

1. 在相同 G 上恢复保存的 R，目标回归必须因预定错误失败，控制测试必须实际通过，
   导入来源与解释器必须匹配新副本。新 G+R 文件清单必须与原冻结清单完全相同。
   Python 版本和已安装包的名称/版本元数据须与原清单匹配，否则保留差异并拒绝放行。
2. 检查保存的 P 只涉及允许的业务路径，记录 `git apply --check` 和实际应用的命令、
   stdin 补丁哈希、完整 stdout/stderr；应用后的全文件指纹必须等于候选的最终清单。
3. 再核对实际导入来源，分别运行准备阶段固定的目标回归及所有控制测试。
   当前 #5998 自动验收固定为清单中的两个 node ID，不等于历史人工验收的 19 项范围。
   必测目标必须有实际通过的用例；任何失败、收集错误、超时、缺失/损坏 JUnit 或跳过项
   都不放行。完整 stdout/stderr（包含警告）和 JUnit、用例数量与跳过原因全部保存。
4. 导入与每次测试后核对完整文件清单、HEAD 和暂存区，保护测试及业务文件不能在
   验收中漂移。最后确认原输入材料未改变，才记录 `independently_verified=true`。
   `observed-files.json` 和报告中的具体差异路径也会保留检查到的失败现场。

退出码 0 仅表示本次完整补丁重放成功，状态为 `independently_verified`；失败退出 1，
保留 `verification_failed`、失败阶段、原因及已产生的日志/副本，绝不自动清理。
原 Agent 状态单独记录为 `agent_status`，例如 `step_limit` 保持不变。这是保存补丁的
独立效果验证，不是重复调用模型的成功率或补丁确定性实验。

本入口要求新管理器的完整运行/导出格式。旧历史补丁和人工报告不会被伪造为新运行，
需沿用原有独立记录；本功能的合成仓库测试不能表述成新的真实 #5998 验收。
验证哈希用于检测材料意外变化，不能抵抗同时篡改所有材料与哈希；测试执行仍非沙箱。
文件核对覆盖已跟踪及未忽略文件，不覆盖依赖安装内容、被忽略文件或瞬时修改后恢复，
不提供跨进程锁。显式非敏感测试环境按准备约定重建，其中 `{run_dir}` 指向新证据目录；
其余继承进程环境，完整 Agent 源码/环境归档与 Agent 工具完整日志仍待后续实现。
`runs/` 和 `experiments/` 仍被 Git 忽略，本机留存不等于远端备份。
