# 测试依据核对与模型预算结算

2026-11-16，依据 [最近单题评测](self-test-quality-model-result-20261116.md) 的真实轨迹实施后续调整。

## 问题与目标

本题的自测先后失败与通过，证明实现改变了行为，但实现和预期共同采用了没有仓库证据支持的格式猜测。第一次 `review` 把猜测记录为假设，却没有引用任务原文，后续同脚本复测也没有重复提交 review。预算运行以每个请求的输入估算加完整输出上限累计计账，Provider 已返回的完整用量未用于结算。预算耗尽时没有专门留出最终报告的请求额度。

目标是让模型能引用任务与固定 G 中的证据，清楚标注假设；同脚本复测复用核对结果，脚本内容变化后失效。模型预算按完整 Provider 用量结算，响应失败、用量不全或重试时采取保守估算，并留出一次结束任务的请求。

## 实施范围

`run_tests.review.criteria` 增加 `expected_basis`，取值为 task、repository、assumption。task 依据需引用所给原始任务，repository 依据附路径、行号和原文摘录，程序从固定 G 校验引用及其 SHA256。assumption 保留为模型假设，并出现在验证摘要中。引用校验仅证明文本和来源存在，不证明模型理解正确或语义覆盖充分。自测仍为 `needs_review`。

同一运行中，相同测试目标和 SHA256 可复用之前的 review。复用的原始来源、警告和假设会重新核验并保存。测试脚本改变后哈希改变，不会命中旧 review。review 继续是可选字段，旧的 `run_tests(target=...)` 调用仍然有效。

`--max-total-tokens` 现在限制运行器的累计预算账本。每次请求预留输入估算和完整输出额度。Provider 给出完整、一致的输入与输出 token 数且本次没有失败重试时，按该次实际用量结算。请求失败、响应不完整、用量缺失或本次有失败重试时，按预留额度计账；重试次数按 Provider 最大尝试数保守放大。Provider 用量只能在响应完成后获知，估算也可能与服务端计数不同，因此这是防止后续请求继续超支的预算门槛，不能保证 Provider 账单绝不超过该值。若 Provider 回报用量超过剩余额度，本轮立即停止，保留已记录的响应和补丁状态。

从当前请求起为最终回答保留一个最大上下文请求及其传输重试额度。正常工具请求必须同时容纳该保留额。模型会看到硬上限、已计账、请求前余额和最终回答预留。剩余预算只够一次请求时，Provider 只收到 `finish` 工具，并要求如实报告完成情况和未解决项。若进度校验拒绝一次普通 finish，原定最终回答预留仍保留给后续请求。预算不足会以 `budget_exhausted` 单独结束，并保存累计计账、预留额度和已报告用量，官方成绩记录会包含该账本。

历史 `maxTotalEstimatedTokens` 配置键继续保留作为兼容别名，新运行增加 `maxTotalTokens` 和 `maxTotalTokenBudgetSemantics`。旧轮次的累计预估值与新账本字段口径不同，不应直接横向比较。当前 Provider 配置默认无传输重试，若开启重试，会在上限内为每次可能请求预留预算。

没有新增 Agent、联网访问、隐藏验收内容或额外模型调用。没有修改 SWE 任务补丁、官方任务、基线或评分输出。

## 定向回归设计与验收状态

2026-11-17 用户要求验收后，使用 coding-agent Conda 运行 test_check_quality、test_evaluation_checks、test_swebench_adapter 和 test_completion。首轮 4 failed、42 passed、12 subtests passed，发现结束后预算预留未释放，以及引文行号、预算场景和模拟 Provider 用量事件的测试材料错误。修正后复跑最终 **47 passed、12 subtests passed**。增加完整 Agent 循环用例，确认开发检查失败后，预算收尾只开放 finish 时仍可报告 incomplete。此轮未运行 Docker 或真实模型。

新增/更新用例覆盖：固定 G 引文及哈希、假设保留为待审、review 同脚本复用和改脚本失效、完整用量结算、缺失用量与失败重试按估算计账、最终请求只开放 finish、预算硬上限阻止 Provider 调用。用例已写入 `tests/test_check_quality.py`、`tests/test_evaluation_checks.py` 和 `tests/test_swebench_adapter.py`。

本轮按项目约定未运行测试、Docker 或真实模型。接下来需运行这些定向回归，并在任何模型评测前做一轮受控小样本验证。单题 `sympy__sympy-15308` 已用于多轮开发，后续不能单独证明跨任务泛化。
