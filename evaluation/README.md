# SWE-bench 小子集接入

生产批次使用 `python -m repo_agent.evaluation.benchmark` 的逐题准入入口。先封存完整任务清单和配对条件，再使用 `preflight` 核验已注册任务，使用 `simulate` 验证队列。正式命令为 `execute`，须指定封存入口锁、SHA-256、其预检目录下的 `official` 输出目录以及允许启动的 arm 数量。见 [生产逐题准入与验收](../docs/v0.6/benchmark-production-admission-20261119.md)。

2026-11-16 最新实现（尚未测试）：启用--agent-tests后，修改会触发自测提醒；声明completed/already_satisfied需要当前业务/检查版本的通过记录，incomplete/blocked仍可结束。测试禁用时不应用该策略。见 [自测策略](../docs/v0.6/evaluation-check-policy-20261116.md)。此前测试数字不覆盖本批。

后续验收：定向检查9 passed/9 subtests及真实容器检查通过，stdout/stderr处理缺口已修复。模型重跑未使用检查工具并明确报告未完成，官方仍未通过；见 [最新结果](../docs/v0.6/swe-lite-developer-checks-result-20261116.md)。下文尚未测试等为历史实施记录。

2026-11-16 新增可选容器开发检查（已实现，尚未测试）：run-agent --agent-tests --check-image <sha256-image-id>。模型可在虚拟.agent-checks/编写Python检查，run_tests在容器执行并回传；也可运行G中已有测试文件。默认仍不运行Agent测试，官方评分独立。详见 [说明与边界](../docs/v0.6/evaluation-developer-checks-20261116.md)。

2026-11-16 已完成首题真实模型与官方评分闭环：模型交付补丁但目标测试失败，120项控制通过；未耗尽扩大后的预算，详见 [运行结果](../docs/v0.6/swe-lite-highbudget-result-20261116.md)。下文未运行等为历史阶段记录。

后续验收更新：正式评分入口已通过本题真实容器基线验收，目标失败 1 项、控制通过 120 项，模型调用 0，见 [真实验收](../docs/v0.6/swe-grader-real-acceptance-20261113.md)。离线回归仍未运行，下文未测试描述保留实施时事实。

当前批次实现 prepare、run-agent、grade、import-grade 四个命令。复用生产编码 Agent 与 TaskRepoTools，默认每次一题、Agent 不运行测试，官方评分独立执行。2026-11-13 指定离线验收 11 passed，CLI 帮助入口 5/5 正常；脚本模型和桩评分进程仅用于工程检查，Docker、真实 Provider 和实际官方任务仍未验收，见 [验收记录](../docs/v0.6/swe-lite-acceptance-20261113.md)。

同日首道真实任务准备发现 Git 配置名称的脱敏误报，修正后联合回归 13 passed。`sympy__sympy-15308` 的材料与干净工作副本已准备；目标基线尚未运行，Docker 最新复核暂不可连接，见 [首题准备](../docs/v0.6/swe-lite-first-task-20261113.md)。

最新：首题官方 G+R 基线已成立，目标失败 1 项、控制通过 120 项，模型调用 0。官方镜像源码与 G 不同、本机 Windows 换行需要处理；本次已用显式运行包装完成，**下述生产 grade 入口已接入两项适配，新增部分尚未测试**，见 [实施记录](../docs/v0.6/swe-grader-adaptation-20261113.md)。整文件另有未计分失败/异常，见 [基线报告](../docs/v0.6/swe-lite-baseline-20261113.md)。上述未运行/连接不可用为此前准备记录。

## 先准备材料

- 使用当前 coding-agent Conda Python 3.12 环境。
- 提供固定 revision 的官方 JSON/JSONL 导出和包含题目 base_commit 的本地源仓库；不自动下载数据或镜像。
- Lite revision 为 `b0dde1093fe417d83b7184254edf8199c1f0dff5`；Verified 为 `78f471bf655a3137b2e8a75af1501690ec009ec3`。本批只记录操作人提供的导出及 SHA256，不在线证明其数据集成员身份。
- 原始任务必须含 instance_id、repo、base_commit、problem_statement、version、patch、test_patch、FAIL_TO_PASS 和 PASS_TO_PASS。gold patch 与测试补丁仅留在 runs 的 private-input 中，不进入 Agent 工作区或问题文本。
- 真实运行前，按项目约定完成 G+R 的目标失败与控制测试核对，准备对应的官方逐题基线报告（含 tests_status），并人工核对环境、解释器和固定版本。代码检查报告中的目标失败/控制成功与冻结测试清单，不会自动替代完整基线准备与环境独立核验。官方空补丁可能直接跳过执行，不能拿跳过报告冒充复现失败。

以下命令中的占位路径和任务 ID 需要替换。说明命令不会自动运行。

## 1. 新建唯一工作副本

```powershell
python -m repo_agent.evaluation prepare --dataset lite --task-file "<official-export.jsonl>" --instance-id "<owner__repo-issue>" --source-repo "<local-source-repository>"
```

返回本轮 artifactDirectory。材料位于 runs/evaluation/swe-bench/run_ID，独立副本按上游项目分类，位于 experiments/owner__repo/workspaces/instance_run_ID_agent。目标仓库只读复制；不覆盖旧轮或复用已修改副本。克隆只提供文件隔离，不构成执行沙箱。

## 2. 显式调用模型

```powershell
python -m repo_agent.evaluation run-agent --run-dir "<artifactDirectory>" --baseline-report "<baseline-report.json>" --max-steps 20 --max-total-tokens 30000 --max-seconds 300 --tokenizer "tiktoken:o200k_base"
```

这条命令会真实调用当前 Provider，必须在获得运行授权和预算后使用。数字是操作示例，不是已经授权的预算。tokenizer 必须与所选模型相配或明确标为估算；执行时间为模型调用边界处检查的软上限，单次请求可超出剩余时间。默认关闭 HTTP 自动重试，累计 token 按估算输入加输出预留计数，失败调用的实际计费可能未知。

沿用现有工具策略，保护测试及测试/构建配置；这属于本 harness 的约束，报告需要披露，不能当作官方基准强制规则。保存源码快照（含本地改动身份）、依赖版本、脱敏输入/工具轨迹、Agent 结果、耗时、Provider 用量、candidate.patch 和 predictions.jsonl。原始费用未知时保留 null。源码快照不宣称与已加载代码逐字节等价。

单轮不可覆盖重跑；失败/中断保留 attempt.lock 和材料，需要重复时重新 prepare。导出使用独立临时 Git index，包含新文件，不改变工作区实际 index；发现被忽略的新文件、受保护测试配置改动、HEAD 漂移或已知密钥时拒绝不完整/不安全导出。模型 finish 不产生 Grade。

## 3. 官方评分或导入既有报告

先预览，不执行测试：

```powershell
python -m repo_agent.evaluation grade --run-dir "<artifactDirectory>"
```

获得测试授权、准备好 Docker 和对应解释器后，安装 evaluation/requirements-swebench.txt 中的官方评分包。不会在导入模块或 prepare 时自动安装。官方解释器必须是 swebench==5.0.2；将 task.json、predictions 和 private-input 等受控材料提供给评分端。

```powershell
python -m repo_agent.evaluation grade --run-dir "<artifactDirectory>" --python "<grader-python>" --timeout 600 --execute
```

`--execute` 先把题目与 predictions 冻结到新评分目录，核对镜像实际源码与固定 G/tree；不符时在派生镜像中恢复并再次校验，再运行官方 harness。镜像必须含 G，否则停止。Windows 启动脚本为 eval.sh/patch.diff 保留 UTF-8/LF，不修改安装的官方包或测试规则。此命令可能下载/构建该题的容器镜像并执行仓库代码；不调用模型。并发固定 1，每次评分使用新 ID，避免官方缓存沿用旧补丁结果。报告位于该次 official/verify_ID 下；固定 5.0.2 的实际逐题日志目录为 logs/run_evaluation。保存实际镜像 ID、源码核对、构建输出、启动脚本/输入哈希及评分脚本/补丁换行记录。探测与评分实例结束后删除，源码恢复构建启用中间容器自动删除。开发检查与评分共用进程锁，占用时拒绝新建，容器暂用 1 GiB 内存上限。非零退出/超时不导入成功成绩，强制中断后清理可能未完成，后续入口检测运行中的评测残留后停止。生命周期改动已通过定向回归，真实容器验收待完成，详见 [资源与清理记录](../docs/v0.6/evaluation-container-lifecycle-20261116.md)。

也可以导入由外部评分流程生成的逐题报告：

```powershell
python -m repo_agent.evaluation import-grade --run-dir "<artifactDirectory>" --report "<official-report.json>" --evaluated-patch "<official-patch.diff>" --test-output "<test_output.txt>"
```

不接受汇总 resolved_ids 代替逐题报告。核对任务 ID、布尔字段及对应补丁；只允许完全相同字节或官方 Windows 文本写入产生的精确 LF→CRLF 变换，保留两份哈希和比较方式。重复导入追加 grades/grade_ID，保留原报告和旧评分。设施失败、补丁未应用及缺报告记为 unknown；官方成功为 pass、有效执行但任务未解决为 fail。导入外部报告仍不证明其环境来源真实，不自动标记 independentlyVerified。

## 当前范围

首批每类 1 题接入、后续每类 3 题案例基线，单次/并发 1；不安排全量。当前批次没有数据下载器、容器内 Agent 安装器、HTTP/前端评估页面、Terminal 或 τ 适配、统计显著性分析或完整价格换算。官方评分环境、数据 provenance 和实际效果仍需后续按授权验收。

方案及版本依据见 [D1 设计](../docs/v0.6/evaluation-d1-integration.md) 和 [评估方案](../docs/v0.6/benchmark-evaluation-plan.md)。
