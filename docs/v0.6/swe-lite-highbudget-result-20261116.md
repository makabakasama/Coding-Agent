# SWE Lite 扩大预算重跑结果

日期：2026-11-16。用户明确选择同题同模型，单次50决策、累计估算50万token、软30分钟、输出8192；工具连接中断轮保留，用户另行同意新开本轮付费运行。

**官方结论：未修复成功。** 模型在预算内正常交付补丁，官方目标测试仍失败，120项控制全部通过。本轮不是预算不足或基础设施失败导致的最终任务结论。

| 项目 | 记录 |
| --- | --- |
| 任务/模型 | sympy__sympy-15308 / gpt-6-luna |
| 本轮 | run_20261116-103934_47c95579 |
| 决策/模型调用 | 16次；HTTP无重试 |
| 模型执行耗时 | 94.66秒 |
| Provider用量 | 输入134723、输出699，合计135422 token；费用未知 |
| 累计估算预留 | 290552 / 500000；每次请求含输出预留 |
| 修改 | sympy/printing/latex.py新增3行，补丁495字节 |
| 官方评分 | resolved=false、infra_failure=false，补丁成功应用 |
| 测试 | 目标test_trace失败1项；控制120通过、0失败 |

补丁新增Trace打印函数，使用 `\operatorname{tr}\left(...\right)`；实际失败断言要求 `\mathrm{tr}\left (A \right )`。从保存补丁与真实断言看，打印格式不一致。没有人工替模型修改，也没有看到评分答案后再追加模型调用。模型自报finished与官方未通过分别保留。

评分首次因Docker管道不存在退出，未运行测试，记录保留在verify_20261116-104111_bd006332。启动Docker后，只重试评分，成功执行轮为verify_20261116-104250_87346ac8，进程退出0并导入fail。实际G/tree已核对、原始题目仅替换image、评分补丁字节一致、LF正确；输入/源码/轨迹及所有评分输出保存。

整份测试文件仍含未计分失败test_print_basic和test_issue_8470/test_issue_13559异常，原始汇总120 passed、2 failed、2 expected to fail、2 exceptions；不宣称全部测试正常。

- [本机摘要](../../runs/evaluation/swe-bench/run_20261116-103934_47c95579/summary.md)
- [机器核验](../../runs/evaluation/swe-bench/run_20261116-103934_47c95579/evaluation-outcome.json)
- [模型补丁](../../runs/evaluation/swe-bench/run_20261116-103934_47c95579/candidate.patch)
- [官方报告](../../runs/evaluation/swe-bench/run_20261116-103934_47c95579/official/verify_20261116-104250_87346ac8/logs/run_evaluation/verify_20261116-104250_87346ac8/coding-agent/sympy__sympy-15308/report.json)
- [导入成绩](../../runs/evaluation/swe-bench/run_20261116-103934_47c95579/grades/grade_20261116-104419_0c4e2140/grade.json)

## 当前意义与后续

SWE Lite单题真实模型、补丁、官方评分闭环已实际跑通，得到一条任务失败结果。首轮小预算终止、第二轮执行中断和本轮正式失败分开记录；这些是同一道已暴露题目的开发尝试，不合并冒充独立pass@1样本或整体能力成功率。

下一步先分析模型看到的问题说明、读取证据、输出格式与反馈方式，再确定需要调整的harness或预算；新代码和新模型试验分别按授权推进。当前没有继续重跑、扩到其他题目或改变冻结测试。独立容器评分不自动等于完整AGENTS G/R/P独立重建验收，imported grade的independentlyVerified仍为false。运行材料为被忽略的本机留存。
