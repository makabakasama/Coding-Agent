# D1：Evaluation 接入设计与只读核对

日期：2026-11-12（北京时间）。初版为 D1 只读调研；同日用户明确要求直接升级现有 Conda Python，并纠正资源规划：仅做固定小子集，不计划全量基准。后续环境升级记录见 status.md；未启动 benchmark 或真实模型调用。

## 选型结论

采用 Harbor + SWE 官方 evaluation harness + τ² 官方 runner。我们开发 Coding-Agent 适配器与统一结果索引，不重写任务评分器。先 SWE Lite，再 Verified、Terminal 和 τ。评测调用生产引擎；适配层不得替换成上游内置 Agent 后冒称本项目成绩。

| 工件 | 本轮确定的版本建议 | 来源与核对状态 |
| --- | --- | --- |
| Harbor | `0.25.0`，源码 `fdba28fc411e47e8765420a1a063b4918fce3def` | PyPI 与 GitHub tag 元数据已查；要求 Python >=3.12；未安装验证 |
| SWE 官方评分包 | `swebench==5.0.2` | PyPI 元数据已查；要求 Python >=3.10；未安装验证 |
| SWE Lite | `SWE-bench/SWE-bench_Lite`，revision `b0dde1093fe417d83b7184254edf8199c1f0dff5` | Hugging Face API 元数据已查；实施时固定 test split、题目清单及哈希 |
| SWE Verified | `SWE-bench/SWE-bench_Verified`，revision `78f471bf655a3137b2e8a75af1501690ec009ec3` | 同上；与 Lite 分开报告，不把重复题当独立新增样本 |
| Terminal-Bench | 首个基线选 `2.0`；上游任务仓库候选快照 `2fd12b88aafdd04a52c298e3940bcb189f9766d6` | 官方 terminal-bench-2 仓库 metadata 已查；实施时核对发布清单与此快照一致性，不把当前 main 快照直接等同正式 2.0 注册包 |
| τ²-bench | `v0.2.0`，源码及随库数据 `f8de30c298689cbe0117d76a378e7315a17e5bd8` | 原 τ² 固定版本，文本 airline/retail/telecom；不是当前 τ-bench 2.0 多语言/语音版 |

以上为设计选型，不是已安装且通过验证的依赖锁。实施时保存安装包及任务工件哈希、镜像 digest 和实际解释器；版本变化另建配置。Terminal 2.0 与原 τ² 用于先建立稳定基线，不宣称代表各系列最新榜单。τ tag 内 pyproject 标为 `0.2.1-dev`，因此必须保留 commit，不单凭包版本判断运行身份。

主要来源：[Harbor 发布版](https://github.com/harbor-framework/harbor/releases/tag/v0.25.0)、[Harbor Python 要求](https://raw.githubusercontent.com/harbor-framework/harbor/v0.25.0/pyproject.toml)、[SWE PyPI](https://pypi.org/project/swebench/5.0.2/)、[Lite metadata](https://huggingface.co/api/datasets/SWE-bench/SWE-bench_Lite)、[Verified metadata](https://huggingface.co/api/datasets/SWE-bench/SWE-bench_Verified)、[Terminal 2 官方仓库](https://github.com/harbor-framework/terminal-bench-2)、[τ² 固定源码](https://github.com/sierra-research/tau2-bench/tree/f8de30c298689cbe0117d76a378e7315a17e5bd8)。

## 本机核对结果

| 项目 | 观察与决定 |
| --- | --- |
| 现有 Conda | 首次核对为 Python 3.11.16；按同日用户授权已原地升级至 3.12.14，并按原版本重装 31 个 pip 包；pip check 与核心依赖导入通过。harbor / swebench / tau2 尚未安装 |
| 框架环境 | 用户明确选择直接升级现有 coding-agent Conda Python 至 3.12，不新增默认评测环境；升级时保留原包清单并刷新对应 Python 版本的二进制扩展。不使用 .venv |
| Docker | 客户端可查询，版本 29.9.2；当前代理读配置/连接 engine 被拒绝，未取得 server 信息，不能判定 daemon 是否运行 |
| WSL | 枚举被拒绝（E_ACCESSDENIED）；发行版及实际 Linux 执行方式尚未核实 |
| 磁盘 | 首次查询时 C 盘约 10.7GB、E 盘约 49.2GB 空闲。整套 SWE 的 >=120GB 建议不作为本项目接入门槛；按选定题目的镜像与运行峰值估算空间，Docker 实际存储位置待核实 |
| 运行方式 | 并发 1，按题准备镜像，不预拉全数据集镜像。不计划全量；新增材料与容器单独登记，不自动清理/迁移历史材料 |

整套资源建议见 [SWE Docker 文档](https://www.swebench.com/SWE-bench/guides/docker_setup/)，不能直接外推为小子集的最低空间。Docker/WSL 权限限制是首次查询的边界，不代表用户环境损坏；Conda 升级单独按用户授权执行。

## 代码适配范围

当前 `repo_agent/__main__.py` 已有 CLI；`Agent.run` 支持编辑任务，`run_conversation` 支持文本和只读工具，但允许动作写死。Provider 固定工具定义，`decisions.py` 的解析和参数校验也绑定固定工具名；`RepoTools` 直接操作本地路径。仅加一个 benchmark 名称配置不足以接入终端和业务环境。

| 工作项 | 推荐实现边界 | 完成条件 |
| --- | --- | --- |
| 工具目录与分派 | 将提供 schema、校验调用、分派动作的接口从固定仓库工具中解耦，默认仍为现有仓库工具 | 现有工具行为保持；容器/业务工具能按运行权限注册，未知工具拒绝 |
| 环境内运行入口 | 基于当前 CLI/引擎提供可被评测框架调用的入口，明确目标工作目录、预算、模型配置和材料路径 | 工作区与 Agent 运行材料分离；仓库测试用任务自己的解释器，不误用 Agent/框架解释器 |
| SWE 适配 | 先以官方补丁 JSONL 接口保证 Lite 判分路径；Harbor 负责的运行/Verified 适配需与所选官方评分版本核对 | 原始 instance_id → Agent trace → patch → 官方 report 可追溯；准备参考解与空补丁检查由评测端执行，不泄露给 Agent |
| Terminal 适配 | 优先在任务容器内装载当前 Agent，外部继承 Harbor `BaseInstalledAgent`；需要容器内 shell 工具及进程状态管理 | 工作目录、输出、退出码、超时、进程状态可查；不把命令落到宿主、不改官方 verifier |
| τ² 适配 | 对 v0.2.0 实现官方 `LocalAgent` 接口，使用 `get_init_state` / `generate_next_message`；模型决策与会话状态复用生产组件 | 工具调用交给官方环境，多轮不重置状态，业务 policy 入上下文；未给业务任务虚构代码仓库 |
| 索引与报告 | 保存框架原始报告，补统一 run/trial 身份、源码/数据版本、预算、材料引用、用量与错误映射 | 原始评分不被归一化覆盖，各基准分别汇总；JSON/Markdown 首版足够，页面后接 |

τ 的每次回调可能只要求生成一个动作/回复，执行工具与用户模拟器调度由官方框架驱动；不能在适配器内部私自消费完整业务工具循环，也不能仅包一层独立 LLM 调用而绕过要评估的 harness 策略。该粒度需要共享单步决策/会话状态接口；编码任务的 finish 与业务对话的终止规则由各环境策略明确，避免硬编码所有领域都调用 coding finish。

接口应保持窄范围：共享模型决策、会话状态、工具 schema/分派及事件，保留环境特有的执行/终止规则。具体变更先从 SWE 最小适配开始，不先重写整个 Agent 或评测平台。

接口依据：[Harbor v0.25.0 installed agent](https://github.com/harbor-framework/harbor/blob/v0.25.0/src/harbor/agents/installed/base.py)、[τ² v0.2.0 agent base](https://github.com/sierra-research/tau2-bench/blob/v0.2.0/src/tau2/agent/base.py)、[SWE predictions 接口](https://www.swebench.com/SWE-bench/guides/evaluation/)。当前仓库的实际实现以所述文件为准。

## 实施与验收安排

1. D2a 首批只做依赖/工件清单、统一结果字段、SWE runner/补丁导出及官方结果导入。任务环境、工件下载、参考解/空补丁检查和真实调用按后续明确授权进行；通过标准是链路与材料正确，不设“Agent 必须解出每道题”。
2. D2a 后续扩展 Verified 与 Harbor 运行适配；再做 D2b Terminal、D2c τ。框架或镜像不兼容作为设施失败保留，不改题目、测试和评分规则迁就当前实现。
3. 规模按小子集固定：每类先 1 题验证接入，再每类 3 题建立初始案例基线（共约 9 个任务；τ 的 3 题分布三个业务域）。每题仅 1 次、仅当前 harness，Verified 先复用 SWE 适配，额外题目另占明确预算；遇到 Lite/Verified 重叠不重复计数。后续有针对性地选 2–3 题做两配置对照或重复试验，不默认全样本多次运行或全量扩展。正式调用前冻结 ID、单题 token/时间限制及整批预算；未给出预算时不自动调用模型。
4. 初始主要报告逐题 resolved/reward、成本、耗时和失败原因；展示成功率时带题数与预算条件，不能与全量排行榜比较。只有实际安排足够重复后才报告 pass@k/pass^k；小样本不作显著性或泛化承诺。
5. Agent 模型、模拟用户模型、评审模型、预算及价格表尚待真实运行批次确定。τ 的模拟用户和 judge 单独计费；所有失败尝试纳入成本覆盖率。harness 比较固定任务和模型，基准 Agent 与改进配置各留独立 trial，单因素消融不同时改变工具与预算。

**D1 当前结论：版本建议、接口范围和小子集验收顺序已形成，Conda 已升级 Python 3.12.14。D2a 已实现并于 2026-11-13 完成指定离线验收：11 passed、CLI 帮助入口 5/5 正常。官方基线准备、Docker/镜像和真实模型仍未验收。见 [验收记录](swe-lite-acceptance-20261113.md) 与 [Evaluation 说明](../../evaluation/README.md)。**
