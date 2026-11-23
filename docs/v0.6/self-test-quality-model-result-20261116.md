# 自测流程优化后的单题模型评测

2026-11-16，按用户“继续启动模型评测”执行一次新模型运行及官方评分。

## 配置与启动检查

任务为 SWE Lite `sympy__sympy-15308`，轮次 `run_20261116-174010_1c83356b`。沿用 gpt-6-luna、最多 50 次决策、累计估算 500000 token、软超时 1800 秒、上下文 128000 token、单次输出上限 8192 token。开发检查单次预算 90 秒，模型传输自动重试为 0。

启动前使用现有 coding-agent Conda 运行 check_quality、evaluation_checks、container_lifecycle、official_runner 四份定向测试，结果为 **37 passed、9 subtests passed**。日志保存在 `runs/engineering/self-test-quality-acceptance-20261116-173742/`。

本轮复用已验收且校验哈希的冻结 G/R 官方基线。模型使用固定 G 的新工作副本，官方评分在新容器核对源码并应用保存补丁。模型前及官方评分应用补丁前均未追加完整 G/R 重测。开发临时脚本另有实际 G/G+P 对照。模型输入使用任务和固定仓库，未提供隐藏官方断言、历史失败轨迹或联网能力。

## 模型与开发检查

模型调用 24 次，耗时 155.188 秒。Provider 报告输入 246737、输出 1706，共 248443 token，其中缓存输入 11648。累计估算预留为 494912 token，第 25 次请求在发送前因剩余额度不足被拒，最终状态为 error，原因为 EVALUATION_TOKEN_BUDGET。实际费用未知。

模型创建临时脚本，对 `Trace(A**2)` 的输出和独立 `A**2` 进行断言。第 16 步首次运行时，G 与 G+P 均断言失败。模型根据反馈修正实现中的双反斜杠，第 20 步再次运行同一脚本，G 断言失败、G+P 通过，对照结果为 assertion_failed_on_base_then_passed。此时开发验证等级为 needs_review。

第 16 步模型提供了 review，列出测试行号和场景，并将 operatorname 格式明确记为假设。但 requirement 字段使用改写语句，未引用所给任务原文，两条摘录均被程序标记无效。后两次 run_tests 没有携带 review。需求映射的传递与引用警告得到实际使用验证，正确引用和语义纠偏仍未完成。第 22 步模型尝试声明 completed，被已有计划一致性校验拒绝，原因是 verify 步骤仍在进行中。不能将该拒绝归因于新增语义校验。

随后模型运行既有 `sympy/printing/tests/test_latex.py`，结果为 120 passed、2 expected to fail、2 exceptions。两个异常均来自 parse_expr(evaluate=False) 的 ValueError，检查分类为 execution_error/unknown。本轮未对这两个异常追加专门的 G 对照，不能单凭此次输出归因于补丁。最终开发验证等级为 verification_incomplete。模型未获得下一次请求机会来处理该结果。

## 补丁及官方成绩

最终补丁 475 字节，仅增加 `sympy/printing/latex.py` 中的 `_print_Trace`，SHA256 为 `d8601525ad5f2c02505bc74ce126fc94f57e5004adae538a818181e9c17da209`。

官方 swebench 5.0.2 评分进程退出 0，补丁成功应用，成绩为 **resolved=false、infra_failure=false，目标 test_trace 失败，120 项控制通过**。导入成绩的验证等级为 external_fail，评分补丁与保存补丁逐字节一致。

实现和自测共同采用 `\operatorname{tr}\left(...\right)`，官方目标要求 `\mathrm{tr}\left (... \right )` 的约定。转义问题得到修正，输出约定仍然错误。该轮再次表明修复前失败、修复后通过只证明版本行为发生了差异，无法独自证明测试预期正确。

## 容器与材料

两次临时检查各依次运行 baseline 和 candidate，既有回归运行一个 candidate，共 5 个开发检查容器。各阶段共享单次检查时间预算，使用现有全局锁串行执行，每个容器使用后删除。官方源码探测、构建与评分沿用串行清理流程。最终 Docker 快照运行数为 0，仅剩原有 6 个已退出容器，本轮无残留。

5 份开发输出归档在本机 D 盘 Docker_temp/check-output，逐份复核存在、大小及 SHA256 一致。未连续采集 Docker 事件或宿主内存峰值，不能报告实测峰值并发或内存改善幅度。

- 模型、轨迹、补丁、开发检查及成绩保存在 `runs/evaluation/swe-bench/run_20261116-174010_1c83356b/`。
- 官方评分轮次为 `official/verify_20261116-174248_a12d50c0/`，导入成绩为 `grades/grade_20261116-174357_f58b2174/`。
- 启动、进程、最终容器快照和归档复核保存在 `runs/engineering/self-test-quality-model-20261116-173948/`。

上述运行材料为 Git 忽略目录内的本机留存。本轮结束后没有追加付费模型重跑。
