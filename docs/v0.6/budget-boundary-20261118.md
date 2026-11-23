# Token Budget 请求边界复核

结论为 **Best-Effort Budget**。500,000 token 在本地累计账本上执行请求准入，Provider 实际消耗受上游计量能力限制。

## 请求链路

1. `Agent.run()` 组装任务、执行历史、工具定义和运行状态。预算层加入余额提示。切到预算收尾时，单独组装精简状态和 `finish`，沿用 1024 输出上限及 128 token 收尾估算余量。
2. `TokenBudget.count()` 计数消息和工具字段，并为消息结构增加估算开销。预算层为一次模型决策预留输入估算、请求的输出上限，以及所有允许的 HTTP 尝试。普通调用还必须留出精简收尾的额度。
3. `BudgetedCodingModel` 从估算到结算持有同一运行的请求锁。进入 Provider 前记录在途预留、累计已结算及收尾预留，需求超限或同一运行已有在途请求时拒绝发送。预算事件用 `budgetRequestId` 关联。
4. Provider 把实际消息、工具定义和 `max_tokens` 序列化成 Chat Completions 请求。客户端独立限制序列化字节数。每次 HTTP 尝试发出前登记，响应到达后先记录用量，再解析工具决策。
5. 有一致的输入与输出用量时按 Provider 实报结算。成功重试用实报值，失败且未知的已尝试槽按一次估算计入，未发起的重试槽释放。缺少完整尝试事件的旧适配器继续保守保留相应估算。明确被本地字节检查挡住的请求计零。结算完成释放在途预留，累计超限时中止并阻止新的调用。

`reservedEstimatedTokens` 是历史累计预留，`inFlightReservedTokens` 是当前已分配给在途请求的额度，`finalAnswerReserveTokens` 是尚需为收尾保留的额度。`accountedTokens` 包含实报及用于未知消耗的估算。另记录已报用量下界和用量未知的尝试数。未查询 Provider 账单。

预算互斥针对**单次运行**，普通任务和评测仍依赖现有任务占用防止同一轮被重复启动。不同任务有独立预算。Provider 的事件回调按执行上下文绑定，两条账本即使共用同一个 Provider 对象也能各自结算。没有新增跨进程共享额度或断点续跑机制。

## 用量口径与 Provider 能力

| 项目 | 口径 |
| --- | --- |
| 输入 | 用 Provider 的 `prompt_tokens` / `input_tokens`。本地输入估算包括工具定义，服务端聊天模板与隐式开销只能由 Provider 确认。 |
| 输出和内部推理 | 用 `completion_tokens` / `output_tokens`。收到 `reasoning_tokens` 时单列诊断；在标准 OpenAI 用量口径下属于输出明细，合计预算中不重复加。兼容服务如何纳入实报总量仍由其接口决定。 |
| 缓存与费用 | `cached_tokens` 单列，仍按总输入记入 token 额度。缓存命中可能改变费用，实际费率以网关和对应模型的账单为准。 |
| HTTP 重试 | 每次尝试可能独立产生消耗。只依据已观测的本地尝试边界结算，网关内部重试以及失败后的远端实际用量仍取决于其回报。 |

本地配置是第三方 OpenAI 兼容网关。当前适配器发送 `max_tokens`，未接入网关的预检计数接口或远端联合额度机制，也没有可核验的证据证明该字段上限覆盖此网关的内部推理。OpenAI 官方 Chat Completions 文档说明 `max_completion_tokens` 限制单次可见输出与推理，Responses 提供独立的输入计数；这些接口的文档能力不自动适用于当前网关与请求。严格 500K 需要同一执行接口可验证的逐次用量上界，覆盖输入、输出和所有尝试，或 Provider 侧原子拒绝超出剩余额度的请求。参见 [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Responses 输入计数](https://developers.openai.com/api/reference/resources/responses/subresources/input_tokens)、[缓存计量](https://developers.openai.com/api/docs/guides/prompt-caching)。

## 自动化证据

使用 Conda 环境运行 `test_coding_budget.py`、`test_provider.py`、`test_provider_retry.py`、`test_provider_stream.py`、`test_chat_token_budget.py`、`test_swebench_adapter.py`、`test_evaluation_checks.py`，共 73 项通过、74 个子用例通过。HTTP 响应由测试替身提供。

- 51 token 预留结算为 46，缓存和推理明细不改变总量。
- 预留 3 次尝试共 180，实际使用两次，本次账本结算为 60 未知估算加 46 实报，第三次未发起。
- 同一账本的两线程有且仅有一次 HTTP 发送；共用 Provider 的两条独立账本各自收到对应实报。
- 紧凑收尾在模拟实耗偏差后累计结算为 500,010，后续没有第二次 HTTP 发送。收尾继续只发送两条消息和 `finish`。该测试证明超限后的行为，Provider 实际上界仍待上游证据。
