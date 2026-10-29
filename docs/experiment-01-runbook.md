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
- 原 seed 副本现标记为 `legacy-unlinked`，当前位置见 `runs/browser-use-5998/INDEX.md`。
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
git -C experiments/browser-use/workspaces/5998_20261026-175230_22903996_agent rev-parse HEAD
git -C experiments/browser-use/workspaces/5998_20261026-175230_22903996_agent status --short
```

需要额外新建实验副本时使用新目录，工具会拒绝覆盖已有目录：

```powershell
conda run -n coding-agent python -m examples.browser_use_5998 --prepare
```

外部仓库使用独立环境 `experiments/browser-use/inputs/env`，避免依赖污染 Agent 环境。
现有环境已迁移并保留原依赖；以下仅供首次安装参考，迁移后无需重装：

```powershell
.\experiments\browser-use\inputs\env\Scripts\python.exe -m pip install --timeout 15 --retries 1 pytest pytest-asyncio pytest-xdist pytest-timeout
```

安装对应历史源码及 pytest 插件后，先执行 Issue 原始复现，记录完整错误、
依赖版本与解释器。依赖导入错误不算缺陷复现。

## 3. 后续验收

1. 在原始工作副本运行独立回归用例，证明缺陷存在。
2. 给 Agent 提供任务描述和测试入口，不给参考补丁。
3. 保存 JSONL 轨迹、补丁、测试反馈和终止原因。
4. 独立重跑回归用例及相关现有测试，检查是否修改或弱化测试。
5. 完成首轮记录后再对照 PR #5999，分析定位、编辑、环境、验证各环节的问题。

Agent 调用使用 `--test-python experiments/browser-use/inputs/env/Scripts/python.exe`
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

独立准备材料位于 `runs/browser-use-5998/prepare_<ID>/repair/`，包括准备清单、冻结的 R、G+R 差异、
文件哈希、环境依赖、导入探针、完整测试 stdout/stderr 和 JUnit 结果。准备失败返回 1，
保留失败阶段、原因、已有日志及副本；不自动删除或重置。新 worktree 位于
`experiments/browser-use/workspaces/5998_<ID>_agent`。ID 为北京时间 `YYYYMMDD-HHMMSS_<8位ID>`；
历史副本已按显式迁移映射重命名，原始证据字节保持不变。

prepare 只自动化准备与基线验证，运行入口另行保存认领状态和 Agent 轨迹；
完整环境归档仍待实现；Agent 允许范围的磁盘源码归档见第 12 节，独立补丁验收见第 9 节。

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

Agent 的测试反馈仍有界；受控运行的完整工具测试日志与源码证据见第 12 节。
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

每次调用在原轮次下按顺序新建 `verify-01/`、`verify-02/` 等证据目录，
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
其余继承进程环境。允许范围的 Agent 磁盘源码与工具测试完整日志见第 12 节；
完整依赖/环境镜像和已加载代码证明仍不在当前实现范围。
`runs/` 和 `experiments/` 仍被 Git 忽略，本机留存不等于远端备份。

## 10. 有界传输重试与真实运行

Provider 在同一次决策请求中，默认最多额外重试 2 次，即总共最多 3 次 HTTP 尝试；
重试前依次等待 0.5 秒和 1 秒。单次 `urlopen` 沿用原有的 `timeout` 设置（默认 90 秒），
不是整轮运行的总时限。构造 Provider 时可通过 `max_retries=0..2` 减少或关闭重试，
本次没有增加 `.env`、模型切换或实验入口配置。

只对类型明确的 TLS EOF、连接重置/中断、响应读取中断和超时重试，包括
`URLError.reason` 包裹的这些异常。仅凭异常字符串提到 EOF 不足以触发重试。
证书验证失败、其他 TLS 错误、所有 HTTP 错误（包括 401/403、429、5xx）、
DNS 等未纳入的请求错误、非法 JSON/响应结构、`finish_reason=length` 均不重试。
非法工具参数仍由现有 `DecisionError` 路径反馈给模型，占用下一决策步修正。

重试复用本次请求已序列化的完整 messages 和 payload，不重新开始 Agent 循环、
截断上下文或重新执行已成功的本地工具。`MAX_STEPS` 仍是决策步数预算，
内部 HTTP 重试不增加决策步数，但会增加请求次数与等待时间。相同请求重发不保证
服务端只处理一次、返回相同决策或只计费一次；这不等于服务端的幂等机制。

日志通过可选的模型事件回调接入 Agent，实验管理代码不处理 HTTP 细节：

- JSONL 首条 `start` 记录 `max_steps`、`step_budget_unit=model_decisions` 和
  `model_runtime.transport_retry` 中的有效尝试上限、单次超时、退避间隔及 HTTP 重试策略。
- `model_request_attempt` 记录当前 `step`、`attempt`、`max_attempts`。失败时立即写入
  异常类型、原因类型、固定原因分类、是否可重试、是否继续以及下一次等待秒数。
  耗尽后的最后一次事件也会保留，随后 Agent 按原行为记录 `error` 并结束本轮。
- `outcome=response_received` 仅表示收到并解析了 JSON 响应，不代表决策合法、工具执行
  成功或修复完成。日志不保存原始异常文本、URL、请求头、Authorization 或密钥；
  最终 `ProviderError` 保留脱敏的异常类型/原因分类及 HTTP 状态码（如有）。

模型/profile/接口和 TLS 证书校验保持原样，不自动切换或关闭校验。重试耗尽仍是失败，
不据此归因于 Python 版本、密钥或某个网络节点。离线模拟通过不能证明实际网络已恢复。

历史失败轮 `20261026T082904Z_dfb6ee2a` 保留原状：前三个只读工具成功，step 4
发生 TLS EOF，未编辑业务代码、未运行测试，P 为空且导出为 `no_code_change`，不应进入
补丁验收。修复请求容错后，真实命令仍由用户自己执行：重新 `--prepare`，
检查新清单，再对新清单执行 `--manifest`。不要重用或清理上述旧失败副本。

## 11. 日常一次启动完整实验

准备好本机模型配置后，由用户自己执行：

```powershell
.\.venv\Scripts\python.exe -m examples.browser_use_5998 --run
```

**`--run` 会真实调用模型 API**。它不接收旧清单，且与 `--prepare`、`--manifest`、
`--check-manifest`、`--verify` 互斥。程序直接调用已有 Python 函数，连续完成：
新 G+R 准备与缺陷复现 → Agent 修复 → 候选 P 导出/完整性审查 → 新副本独立验收
→ 最终总报告。准备失败时不会构造 Provider，也不会启动模型。

总报告从启动开始保存在新的
`runs/browser-use-5998/run_<ID>/experiment.json`，分阶段原子更新；同轮保存中文 `summary.md`。
准备与 Agent 材料放 `repair/`，首次独立验收放 `verify-01/`，共用一次分配的 ID，通过报告中的 `prepare_manifest`、
`workspace`、`agent_record`、`candidate_report`、`verification_report` 和
`verification_workspace` 精确关联；对应副本为 `5998_<ID>_agent` 与 `5998_<ID>_verify-01`。
后续 `--verify` 追加 `verify-02`，不修改原 `experiment.json` 的历史成功事实。
准备初次落盘就提供路径回调，所以即使准备途中中断，总报告也能指向对应清单与副本。
终端最后输出总报告及其路径。

自动验收的门槛只看导出结果：`status=candidate_exported` 且
`eligible_for_verification` 严格为布尔 `true`。Agent 正常 finish、`error`、
`step_limit` 或外层运行异常，只要留下通过审查的 P，都会进入独立验收；不会依赖
Agent 的 `last_test_passed`。例如修复后遇到 HTTP 429 结束，报告仍可同时显示
`agent_status=error`、`verification_status=independently_verified`，原失败原因保持不变。

空 P、保护文件违规、导出失败或启动拒绝时，保存 `verification_status=skipped` 和
具体的 `verification_skip_reason`；已有清单、轨迹、部分材料与副本均保留。
用户 Ctrl+C 是停止请求：Agent/导出后不再自动启动验收；验收内部已中断也不会补跑。
导出中断不会将已经完成的 Agent 状态改写成模型失败，另记中断阶段。

`--run` 的退出码按最终独立验收定义：

| 退出码 | 含义 |
| --- | --- |
| `0` | 完整独立验收通过，即使原 Agent 状态为 `error` 或 `step_limit` |
| `1` | 准备/运行/导出/验收失败，或因没有合格 P 而跳过验收 |
| `130` | 用户中断，停止后续阶段 |

总报告分别保留 `agent_status`、`agent_exit_code`、`export_status`、
`verification_status` 与 `verification_result`，不覆盖原 Agent 或原验收报告来制造成功。
因此总退出码 0 仅证明保存补丁的完整独立重放通过，不证明模型正常结束、整个项目无误
或模型重复运行仍会成功。原 `--manifest` 保持单阶段行为，其退出码不等于独立验收结论。
手动阶段入口仍可用于调试，`--verify <原清单>` 可用于复验已保存候选，重复验收继续新建副本。

串联入口不增加 Docker、断点恢复或新测试范围；固定测试、G/R/P
检查、有限网络重试与非沙箱限制均沿用既有实现。总报告不代表 `runs/` 或 `experiments/`
已备份到远端。离线临时仓库测试通过不能冒充新的真实 #5998 运行。

## 12. 新运行的测试日志与 Agent 磁盘源码证据

以下功能仅对加入它们之后的新运行生效。历史成功轮
历史成功轮现位于 `run_20261026-175230_22903996`，原 `pipeline_20261026T095230Z_22903996` 的字节证据保持原样，不能声称该轮已经保存
下面新增的日志或源码快照；本轮开发没有回填或重跑那份材料。

`run_prepared` 为手动 `--manifest` 和总控 `--run` 默认启用同轮 `test-logs/`，
目录位于证据目录内、被测 worktree 外。通用 `RepoTools` 构造仍默认不写日志，
需要时可显式传入 `test_log_dir`；被测仓库内部的日志目录会被拒绝。
每次 `run_tests` 使用新的 `test_<UUID>/`，保存完整 `stdout.txt`、`stderr.txt` 和
`metadata.json`：实际命令、cwd、测试解释器、超时、退出码、是否超时/中断、各流字节数
及 SHA256。超时保留异常携带的部分输出，bytes 原样保存、str 按 UTF-8 编码。
移除 `--disable-warnings`，使 pytest 警告正文也保存在完整日志中。

模型仍只收到合并输出末尾最多 6000 字符、`output_truncated`，以及 `test_log` 的
ID、文件路径、大小与哈希；完整输出不进入模型上下文，也没有新增工具或改变决策协议。
日志初始化失败时不启动测试；测试完成后落盘失败则 `passed=false`，同时保留实际
`exit_code` 和 `logging_failed`，不能把退出码 0 伪报为证据已记录的通过。
若磁盘不可写，已有部分文件保留，记录 `write_failed`，不自动清理或复用旧 ID。

日志元数据只保存显式测试设置中的固定允许项：`ANONYMIZED_TELEMETRY`、
`BROWSER_USE_CONFIG_DIR`、`PYTHONHASHSEED`、`PYTHONUTF8`、`PYTHONIOENCODING`、
`PYTEST_DISABLE_PLUGIN_AUTOLOAD`；不会复制整个进程环境或配置密钥。测试进程仍沿用
原来的环境继承方式，输出流原样保存，当前不提供对测试自身打印内容的自动脱敏或隔离。

模型启动前在同轮 `agent-source/` 独占创建初始快照。源码根从归档模块的实际
`__file__` 确定，不跟随实验夹具或入口中的 `PROJECT_ROOT`：只复制非隐藏的
`repo_agent/*.py` 直接子文件和 `examples/browser_use_5998.py`，保留相对结构、
逐文件字节数/SHA256。未提交和未跟踪的允许范围源码也会复制；符号链接/重定向路径
不进入快照。不会遍历嵌套目录，不复制 `.env`、`.git`、实验材料或整个工作目录。
清单另存可用的 Git HEAD、仅相关源码的未提交/未跟踪状态，以及执行 Agent 的
Python 路径/版本、平台和 OpenSSL 版本。非 Git 来源或 Git 元数据不可用时如实标注，
不因此放弃已经捕获的磁盘源码。

初始捕获失败或捕获期间源码已漂移，不启动模型。运行后写入新的
`source-check_<UUID>.json` 检查磁盘源码及初始快照是否改变，保留原快照与最初哈希，
不静默覆盖。最终 `agent-run.json`、工具轨迹与总报告可通过 `agent_source`、
`agent_source_check` 和 `test_logs` 定位材料。
`evidence_status=recorded` 表示本范围证据已记录；`source_drift` 表示源码发生漂移；
`incomplete` 表示捕获、检查或日志存在缺口。这些字段与 Agent/补丁验收状态分开，
不抹去原运行结果；完整补丁验收通过也不能掩盖 `incomplete`。

这是磁盘源码快照，不是进程内存、已加载字节码或完整测试环境/依赖镜像。
运行后的检查不是持续文件锁，不能识别改动后又恢复的瞬时变化，也不承诺磁盘快照与
进程已加载源码绝对一致。没有新增 Docker、依赖导出系统、加载 hook、限流或上下文压缩。
