# Agent benchmark 与 harness 对照评估方案

日期：2026-11-12。根据用户对原先“三个简单样本作为评估主线”的纠正重新整理。本页是调研与方案，未安装评测框架、下载数据集、运行测试或调用真实模型。

## 当前选型与接入计划（用户指定范围）

D1 已形成 [具体版本、环境核对与接口方案](evaluation-d1-integration.md)，Conda 已升级 Python 3.12.14，规模为固定小子集。D2a 已实现 SWE 本地任务、Agent、补丁与官方评分接口；2026-11-13 [指定离线验收](swe-lite-acceptance-20261113.md) 11 passed、CLI 帮助入口 5/5 正常。Docker、真实模型、实际官方题目环境尚未验收，工件/镜像冻结待准备；操作见 [说明](../../evaluation/README.md)。

用户确定首期覆盖三类：SWE-bench Verified / Lite、Terminal-Bench、τ²-bench / τ-Bench 系列；指标与受控 harness 对照沿用本文。Pro 与 METR 本期只保留研究背景。Verified 和 Lite 为不同任务清单，分别报告，不把可能重叠的题重复计入合计成功率。

### 现成系统优先

| 范围 | 推荐复用 | 自有实现边界 |
| --- | --- | --- |
| SWE-bench Verified / Lite | 优先 Harbor 已有 SWE 适配；Lite 的所选版本/任务映射需核对，必要时使用 SWE-bench 官方 harness | 调用当前 Agent、产出补丁和用量、导入官方结果；校验 Harbor 与官方验收差异 |
| Terminal-Bench | Harbor 的任务环境、调度、verifier、轨迹及结果设施 | 容器内 Coding-Agent 入口和工具执行适配；不重写终端任务判分 |
| τ² / τ 系列 | 优先对应冻结版本的官方 runner、用户模拟器、业务工具及 evaluator | 自定义 Agent 接口适配和结果导入；目前未确认 Harbor 对所选 τ 版本有协议等价适配，不预设三套全走 Harbor |

依据：[Harbor SWE Verified 与 parity 记录](https://hub.harborframework.com/datasets/swe-bench/swe-bench-verified/latest)、[SWE 官方评估入口及补丁格式](https://www.swebench.com/SWE-bench/guides/evaluation/)、[Harbor 自定义 Agent](https://docs.harborframework.com/agents/custom-agents)、[τ 官方 Agent 接口](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/agent/README.md)。官方已有 parity 记录不证明本项目选定版本/环境自动等价，需要保留本次核对结论。

采用“现成执行/验收设施 + Coding-Agent 适配器 + 本地统一索引与汇总”。评测直接调用生产 Agent 引擎，UI 不必参与每道题；Chat 排队、刷新、流式等产品行为仍由已有工程回归覆盖。不能换用评测框架内置 Agent 后把分数记到 Coding-Agent 名下。

### 接入顺序与完成条件

1. **D1 版本与协议锁定**：固定 SWE Lite/Verified 清单与 revision、Terminal-Bench 版本、τ 文本任务版本、框架版本、容器镜像、资源/网络权限和结果格式。建议 Lite 先接通再扩展 Verified；Terminal-Bench 选择一个固定发布版，不在实验中追踪 latest；τ 首批文本模式，不含语音。τ 仓库已有多个世代，须确认是原 τ² 固定 commit 还是后续文本发布版，不能混报成绩。输出依赖和适配差距清单。
2. **D2a SWE 适配**：在隔离任务环境运行现有编码引擎，导出补丁供官方验收。先用已知参考产物与空补丁核对验收设施，再接真实 Agent。参考解和隐藏验收材料不进入 Agent 输入；路径授权不依据 gold patch 推导。完成标准为任务到轨迹、补丁、官方评分和资源记录的完整关联，而非要求 Agent 解出每一题。
3. **D2b Terminal 适配**：补容器执行后端及终端工具，具备工作目录、输出、退出码、超时和进程状态处理。所有命令在任务容器内执行，不能绕回宿主。复用相同决策/上下文引擎及 Harbor verifier，运行结束保存产物与终止原因。
4. **D2c τ 适配**：引擎接受环境提供的工具 schema 与分派器；业务工具调用交还官方环境，跨用户轮保留状态，由官方用户模拟器推动对话。固定模拟用户模型/配置，并把其费用和 judge 费用与被测 Agent 费用分开记录。完成标准为真实调用 Coding-Agent 策略、官方工具和官方 evaluator，而非仅运行官方基准 Agent。
5. **D3 对照与报告**：三套结果保留各自原始评分，只统一任务身份、模型/harness 版本、attempt/trial、预算、用量、耗时、材料和失败分类。分别报告成功率/resolve、pass@1、pass@k、pass^k（重复次数足够时）、成本/时延及置信区间，不生成跨三类的任意综合分。先固定模型比较 harness，再固定 harness 比较模型；消融一次改变一个因素。

宿主沿用现有 coding-agent Conda 环境，由用户授权直接升级 Python 至 3.12；任务运行使用 Linux 容器。Docker/WSL 实际可用性和所选题目镜像峰值空间待预检；不自动改用 .venv，不默认新增环境。官方工具推荐的包管理器不自动成为本项目的新环境约定。

每类先 1 题接入，再每类 3 题形成初始案例基线；每题一次、并发 1，不计划全量。后续仅对 2–3 个针对性样本安排配置对照或重复运行，另行明确预算。真实调用前确定 Agent 模型、τ 用户模拟模型、单题与整批预算。断点恢复跳过已完成 trial；模型部分输出/有副作用的失败不默认自动重跑，新增尝试独立留档。

当前固定工具 schema 和 RepoTools 的宿主文件系统实现不能直接覆盖三类环境。共同需要一个窄的工具定义/分派接口，让相同引擎接本地仓库、任务容器和 τ 环境；避免在适配器里重建另一套 Agent 循环。若某项确无现成支持，仅补该适配、指标或索引缺口。现阶段先交 CLI/配置与可复核报告，工作台评估页面后接。

本次为接入讨论与计划同步，未开工、安装、启动环境、运行测试或调用模型。

## 评估对象

三个手写场景仅作产品回归，不代表能力基准。主线改为公开基准上的任务结果和 harness 对照实验。模型比较固定 harness；harness 比较固定模型快照及推理设置、任务、资源、网络权限与验收器。全系统对照可比较不同工具，但必须披露工具差异；研究某项上下文策略则保持其他条件相同。

agent harness 是提示、上下文、工具、循环、预算与恢复等运行机制；evaluation harness 是任务环境、调度、外部验收和统计设施。二者分开，避免让 Agent 自己给自己判成功。[Anthropic 方法说明](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)。

## 数据集候选与适用范围

| 基准 | 测什么 | 对本项目的用途 |
| --- | --- | --- |
| SWE-bench Verified / Lite | 固定历史仓库上的真实 issue 修复，官方测试判定 | 成熟的编码基线与适配入口；不是无污染的前沿能力证明 |
| SWE-Bench Pro | 更复杂的仓库级修复/功能任务 | 主要编码能力候选；需固定发布版本和官方协议，先审查当前工具及环境能否支持 |
| Terminal-Bench | 隔离终端环境中的真实技术任务，外部 verifier 验收 | 后续通用工具与长流程能力候选；当前没有通用 shell 能力，不能宣称可直接完整接入 |
| τ²-bench / τ-Bench 系列 | 多轮用户、工具与环境状态交互 | 会话与业务工具能力参考；领域不是代码仓库编辑，非首批编码主基准 |
| METR time-horizon suite | 按人类专家完成任务的时长刻画能力难度 | 长任务能力方法参考；需额外人类基线，不是统计 Agent 自己运行了几小时 |

来源：[SWE-bench 官方](https://www.swebench.com/verified.html)、[SWE-Bench Pro V2](https://labs.scale.com/leaderboard/swe_bench_pro_public_v2?tab=full)、[Terminal-Bench](https://www.tbench.ai/)、[τ-Bench 官方仓库](https://github.com/sierra-research/tau2-bench)、[METR 方法](https://metr.org/time-horizons/)。

版本不能混用。官方当前 Pro 页面标有 2026-09-22 V2 修订：642 个任务、剔除无效任务及重做部分说明，并在干净环境重评补丁；页面下方仍保留旧数据集介绍，计数以所固定发布工件为准。Terminal-Bench 官网当前标题为 4.1；文献中常见的 2.0 是另一个固定版本，不默认“最新版”和旧榜单等价。

公开基准也可能受污染、环境失效或评分不公影响。[OpenAI 对 Verified 的审计](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)和[对 Pro 的审计](https://openai.com/index/separating-signal-from-noise-coding-evaluations/)说明不能把榜单分数直接当真实开发能力。这些是特定版本的研究结论，不直接外推到所有后续修订版。保留新的真实需求和时间切分任务作补充，开发时接触过的失败样本不算独立保留集。

## 指标

| 维度 | 指标及解释 |
| --- | --- |
| 最终能力 | Resolve / success rate；编码任务修复 fail-to-pass 且 pass-to-pass 不退化才算 resolved；按基准原始规则评分 |
| 一次成功 | pass@1：一次完整任务运行成功的概率；一次运行内部可含多次模型/工具调用 |
| 多次机会 | pass@k：k 次独立尝试中至少一次成功；需披露是否允许实际选择正确答案，不能当单次部署成功率 |
| 稳定性 | pass^k：k 次均成功；与 pass@k 含义相反，不能用多试取最好掩盖不稳定 |
| 成本效率 | 每个计划任务及每个解决任务的费用、输入/输出/cached token；失败尝试费用纳入，未知账单不写零；画成功率对成本预算曲线 |
| 时间效率 | 完成延迟 P50/P95、超时率；交互另看首个文本时间，分别记录排队、模型请求、工具执行 |
| harness 诊断 | 协议错误、无效工具调用、重复读取、恢复失败、权限违规、工具错误和上下文证据丢失；诊断指标不替代最终成功率 |
| 可信度 | 样本数、重复次数、置信区间、基础设施错误与未运行数、数据与评分器版本 |

pass@k / pass^k 区分见 [Anthropic](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)；Resolve 定义见 [Pro 官方协议](https://labs.scale.com/leaderboard/swe_bench_pro_public_v2?tab=full)。缓存占输入的比例仅是诊断指标，应同时观察费用与延迟，且披露冷/热缓存运行条件。

本项目建议按任务分层汇总成功率，并对同一任务的配置差异做配对 bootstrap；同任务重复运行作为一组，避免当作相互独立的新题。主报告保留全部计划任务分母，同时另列模型/Agent失败、基础设施错误、未运行和仅可判定任务结果；跨系统排名采用同一处理规则。

## 实验与下一步

1. D1：确定一个官方编码基准的准确版本、官方验收路径和环境需求。审查 Coding-Agent 适配缺口，再设计薄适配器；优先复用官方 evaluation harness，Harbor 是候选执行设施而非数据集。不得为迁就当前工具而降低官方验收要求。
2. D2：固定模型，在相同任务上比较当前 harness、基准 harness（如 mini-SWE-agent）和一个单因素消融版本。若工具能力不同，标为全系统对照，不把差异全归因于某项上下文策略。预先固定预算与停止规则。
3. D3：仅做预先固定的小子集案例评测，初始每类 3 题、每题 1 次；撤回此前 20–50 题 × 3–5 次的工作量建议。不默认扩大到全量，选择任务时记录仓库/任务类别和环境成本，不能按成功结果事后挑题。逐题结果优先，不把少量任务成功率与全量官方榜单直接比较。

上下文方面优先考察历史工具证据选取、压缩策略与稳定前缀的消融；当前文件变化、权限和失败重试用现有回归集守住。公开编码题不覆盖多轮产品体验，保留针对性的多轮真实任务作为另一套报告。能力、可靠性和成本联合判断，不追求单一更低 token 或更短轨迹。

环境需隔离并保护验收材料；模型只获得题目及任务允许的工作区，评分时依据基准协议在干净环境复验产物。保存每轮轨迹、源码身份、配置、补丁和失败。参考 [Harbor 自定义 Agent 接口](https://docs.harborframework.com/agents/custom-agents)与[外部 verifier](https://docs.harborframework.com/tasks/verifier)。本轮没有开工或执行评测。
