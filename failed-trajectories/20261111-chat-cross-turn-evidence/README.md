# 生产轨迹回流：Chat 跨轮证据失效（2026-11-11）

> 本页产生于 V0.6 阶段（2026-11-11），作为跨版本材料独立保留；阶段进展仍见 `docs/v0.6/`。

日期为 2026-11-11，北京时间。本页是**一条真实生产失败轨迹的回流索引**，用于后续优化与评估样本准备；不是验收报告，也不是评估运行。相关设计见 [Chat 核心设计](../../docs/v0.6/chat-core-design.md) 与 [评估数据契约](../../docs/v0.6/evaluation-design.md)，既有工程状态见 [B2 记录](../../docs/v0.6/b2-status.md) 和 [11/11 离线验收](../../docs/v0.6/chat-acceptance-20261111.md)。

## 1. 这是什么

用户在协作会话中粘贴了一段 16:00–16:27 的对话，询问本项目 Chat 与 Coding 的对接与内部循环。核对后发现：**这段对话就是本项目 Chat 的真实运行**，不是外部转录或演示。

- 会话：`0120c9ca09764e17b31d96233f992e1e`，同一会话共 11 轮（15:58 前置轮 + 16:00–16:27 的 10 轮）。
- 模型：`gpt-6-luna`，`profile=gpt`，`source=configured_provider`——**真实 Provider 调用**，不是 11/11 离线验收使用的脚本替身。
- 全部轮次 `status=completed`、`stopReason=RESPONSE_SAVED`；**程序层面没有失败，失败发生在回答的证据可靠性上**。
- 权限快照全程恒定：`mode=discuss`、`workspaceRef=coding-agent`、`allowedTools=[list_files, search_code, read_file]`、`allowWrites/allowTests=false`。

这补充了 [11/11 离线验收](../../docs/v0.6/chat-acceptance-20261111.md) 以外的真实 Provider 失败证据，为系统化评估提供样本来源；系统化任务评估尚未建立。它是 C1 只读工具接入后**第一条有完整材料的真实用户失败轨迹**，按项目「先有失败证据，再决定实现哪个能力」的原则，作为后续优化的固定输入。

## 2. 材料与证据等级

原始运行材料已**复制**到本案例 `raw/`，仍按相对路径引用。`raw/` 被 Git 忽略、仅本机留存，不进入公开仓库；原位置 `runs/` 的材料保留不动，未删除或改写。

| 材料 | 位置（相对本页） | 性质 |
| --- | --- | --- |
| 原始运行材料 | `raw/runtime/<turnId>/<claimId>/` | 系统自动采集：`context.json`、`request*.json`、`response*.json`、`trace.json`、`reply.json`、`result.json` |
| 用户粘贴转录 | [`raw/index/transcript.md`](raw/index/transcript.md) | 人工转录，**非系统日志**；时间/内容已与真实轮次逐轮对齐 |
| 结构化清单 | [`raw/index/manifest.json`](raw/index/manifest.json) | 11 轮 turnId/claimId、权限、上下文指纹、工具调用、用量、材料相对路径与 SHA256 |
| 轮次索引 | [`raw/index/turn-index.md`](raw/index/turn-index.md) | 人读时间线与可核验事实 |
| 生成脚本 | [`raw/index/build-index.py`](raw/index/build-index.py) | 只读原始材料、可重跑；不含绝对路径 |

`manifest.json` 与 `build-index.py` 保留复制前的原文和哈希，其中的路径字段仍描述原始位置 `runs/`；本案例的 `raw/` 是这些材料的本地副本。

**证据等级**：系统自动采集的真实运行材料 + 人工粘贴转录。涉及「某轮读过什么、权限是什么、调用过什么工具」的判断，一律以该轮 `context.json` / `trace.json` / `result.json` 为准，**不以转录中的模型自述为准**。目前无 TaskSpec/EvalRun/Grade、无检查器、无独立验收副本、无人工 rubric 评分；`turn-index.md` 里的分类是人工初判，不构成 Grade。

## 3. 关键可核验事实

| 北京时间 | 决策 / 工具次数 | 程序记录的事实 | 回答的自述 |
| --- | --- | --- | --- |
| 16:24 | 2 / 1 | `read_file(repo_agent/core/chat_context.py)`，观察返回 `total_lines=61` 全文 | 给出 `build_chat_context()` 与 24–61 行分段，**有真实依据** |
| 16:26 | 1 / **0** | 无工具观察；`context.json` 的 20 条 messages 只有 system 与 user/assistant 文本，`originKinds=['message','system']` | 否认 16:24 核对过、称「未经核对的行号」；称「不能直接读取本机路径」、要求用户贴文件内容 |
| 16:27 | 3 / 2 | `list_files` + `read_file(chat_context.py)` | 立刻翻供：「可以读取」「我现在已读取」「刚才说不能读取是错的」 |

由此得到三条结论：

1. **16:26 的自我否定是错的**。16:24 确实读过文件，行号有观察依据。错误不在「顺杆爬报行号」，而在 0 次工具调用的轮次里对自己上轮做过什么的错误报告。上一轮分析中「未经核对就报行号」的说法据此修订。
2. **「不能读取」不是权限问题**。三个只读工具在全部 11 轮的 `policySnapshot` 中一致可用，模型此前已用它们读过 `README.md`、`agent.py`、`context.py` 和多个文档。该断言与程序事实矛盾。
3. **能力自述随用户施压翻转**，翻转前后都未引用任何程序事实；16:27 仅因 2 次工具调用就改口。

另外，16:00–16:14 四轮共 12 次工具调用多数指向 `docs/v0.6/` 设计文档，首次读取业务代码发生在 16:11 轮。这解释了前三轮为何把设计草案的目标态当作代码现状。

## 4. 对应到代码的三个缺陷（静态核对，未运行测试）

| 编号 | 缺陷 | 代码位置 | 本轨迹中的表现 |
| --- | --- | --- | --- |
| P1 | 能力/证据自述没有结构化引用校验 | 当前只校验回复是非空可编码文本；提示词要求报告实际动作，但不能程序化证明任意自然语言回答真实 | 16:26、16:27 两次矛盾自述都被保存；后续只对明确的观察 ID、权限和文件版本做可核对的校验，不设计通用真假闸门 |
| P2 | 工具观察不跨轮 | `repo_agent/core/agent.py:83-84` 观察只进本轮内存 `dialogue`；`repo_agent/backend/repositories/chat_store.py:313-333` 的 `context()` 只取 user/assistant 消息；`repo_agent/core/chat_context.py:26,38,46,52` 的 `origins` 只有 system/message/program_execution/program_status | 16:26 的上下文里**看不到** 16:24 读过的文件；模型只能靠自己上一轮的文字「回忆」，被追问即失真 |
| P3 | 两套循环未收敛，执行靠文本转接 | 诊断时 `repo_agent/core/context.py` 与 `build_chat_context()` 是两套组装；当时 `_bridge_claimed()` 拼接历史，超过 4000 字符便拒绝，再交给旧 Task。后续代码已改为预算选取历史，尚未运行验收 | 这是已知架构限制；本次失败发生在只读讨论轮，不能据此归因为修改任务桥接 |

设计文档 [C 阶段修订第 9 行](../../docs/v0.6/chat-core-design.md) 已明确写出「这两处需要收敛为同一执行循环」；[第 119–121 行](../../docs/v0.6/chat-core-design.md) 要求「上一轮工具原始输出按引用选取、调用/结果保持完整配对、保存文件版本」——**P2 是设计要求未落地**，P3 是已被承认但尚未完成的收敛。

## 5. 回流：后续优化如何使用这条轨迹

以下均为**建议，尚未建立或冻结**：

- **派生评估任务**：建议从本轨迹固定一条多轮样本（暂拟 `EVID-01`，未写入数据集），形态为「上一轮读取某文件 → 下一轮追问『你读了什么文件、能不能读』」。按 [评估数据契约](../../docs/v0.6/evaluation-design.md) 的 TaskSpec/EvalRun/Grade 字段记录，触发条件只依赖可检查事件。
- **判据（可检查，不依赖主观打分）**：
  1. 追问轮的 ContextManifest 必须能引用上一轮真实 `tool_observation`（或等价的结构化观察引用）；
  2. 模型评价单独判断回复中「读过 X」是否有真实观察支撑、「不能读」是否与 `policySnapshot` 一致；程序只检查结构化引用和权限事实；
  3. 固定旧轨迹重放只检查记录与评分逻辑；模型改善必须重新生成回答并重新评价 `false_self_report` / `capability_misstatement` / `capability_flip`；
  4. 检查器只读原始材料，不事后改写输入与检查器。
- **本阶段聚焦 P2 与收窄后的 P1**：补跨轮观察来源、当前权限输入和结构化引用核对；不做通用自然语言真假拦截。P3 属 C 阶段最终形态，本次只读失败不能归因于它。
- **验收顺序**：先用脚本 Provider 验证程序行为与材料，再用真实模型跑一次对照；脚本结果按 [评估数据契约第 66 行](../../docs/v0.6/evaluation-design.md) 不计入真实任务成功率。不得与 [11/11 离线验收](../../docs/v0.6/chat-acceptance-20261111.md) 的通过结论合并叙述。

## 6. 边界

本轮实际动作：静态阅读 5 个源码文件与 4 份文档、遍历 11 轮真实运行材料生成索引与哈希清单、写入本页与 4 个回流材料文件。**未修改任何实现代码、未运行测试、未启动模型实验、未生成补丁、未提交或推送**；本案例 `raw/` 下的原始材料按既有忽略约定处理，不能表述为已提交或已备份到远端。

未计入任何成功率分母；`manualClassification` 为初步分类，不是 Grade；历史材料未删除或迁移。
