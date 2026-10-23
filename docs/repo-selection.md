# 2026-10-19 GitHub 实验仓库比较

> 目的：选择 Coding Agent 的真实回归实验仓库，并观察哪一类问题更适合进一步做工程项目。
> 开放 Issue 数量是检索时的近似快照，**不代表可认领的 Issue 数量**，状态可能随时变化。

| 维度 | browser-use/browser-use | pydantic/pydantic-ai | langchain-ai/langgraph |
|---|---|---|---|
| 方向 | Browser/Tool Agent | 通用 Python Agent SDK | 状态化 Agent 框架 |
| Issues 快照 | ~180 | ~1,023 | ~415 |
| 最近 Issues | 2026-10-17 工具与文件系统问题 | 2026-10-19 Harness、Schema、Tool Args 问题 | 2026-10-17/18 State、ToolNode、Checkpoint 问题 |
| 活跃度初判 | 高，近期仍有 Issue 与 PR | 很高，问题和 PR 变化快 | 高，有新问题与活跃维护 |
| 测试基础 | pytest、pytest-asyncio、pytest-httpserver、tests/ci、浏览器集成测试 | pytest、anyio、模块级单测、需区分 live markers | 多包 monorepo，libs/* Makefile / 模块测试 |
| 独立复现成本 | 单个工具执行问题较低；全浏览器中高 | 部分纯 Python 问题低；模型适配高 | 核心 Graph 问题中；服务/DB 问题高 |
| Coding Agent 可切入点 | keyword-only Tool 参数；误报成功；错误信息 | 参数类型契约、Agent 状态、兼容性 | 错误处理、状态、类型注解、Checkpoint |
| 初期建议 | **第 1 个实验** | 第 2 个实验 | 第 3 个实验 |

## 候选真实 Issue（确认链接后再 clone）

### A — browser-use
- [#5998](https://github.com/browser-use/browser-use/issues/5998) keyword-only 自定义 Tool 参数传参错误，能只通过 Python 复现，不用浏览器，不用 LLM。**作者已开 [#5999](https://github.com/browser-use/browser-use/pull/5999) 修复 PR**：适合作为具有参考修复方案的已知答案实验，**不应冒充开放贡献机会**。
- [#6031](https://github.com/browser-use/browser-use/issues/6031) 一组 Agent 误报完成的评测用例，评测基于服务端状态而非回答文字；已有外部贡献者参与准备，需要避免重复劳动。可用来学习“任务成功必须有环境证据”。
- [#6003](https://github.com/browser-use/browser-use/issues/6003) 页尾换行导致分页提取异常，需再检查重现与 PR 状态。

### B — pydantic-ai
- [#10525](https://github.com/pydantic/pydantic-ai/issues/10525) `python -O` 删除 assert 后，非对象 JSON 破坏 Tool Args 的字典契约。无模型需求，但**已有 #10491 修复 PR 相关工作**，只能先做已知答案实验。
- [#10530](https://github.com/pydantic/pydantic-ai/issues/10530) XML 格式化把输入键直接当作 tag，已有其他贡献者表示在处理，不推荐抢 PR。

### C — langgraph
- [#9338](https://github.com/langchain-ai/langgraph/issues/9338) postponed annotations 下 ToolNode error handler 对 union 类型解析不正确。带最小复现，适合训练代码定位与单元测试。
- [#9327](https://github.com/langchain-ai/langgraph/issues/9327) callable-class Router 的 Literal 返回类型未被识别；可作为第二批候选，需复查近期 PR 和行为。

## 选仓原则
1. 首个实验不要求修复一个未被解决的项目 Issue；先验证我们自己的 Agent 工具闭环。
2. 后续 PR 优先挑选有最小复现、无已在进行的 PR、可以跑局部测试、维护者仍活跃的问题。
3. 每次实验必须记录：仓库 commit SHA、依赖、OS/Python、失败测试、补丁、成功测试、模型、步骤数、Token/耗时（可获得时）。
4. 不拿 Issue 数量、Stars 或项目名充当技术成果。

## 官方地址
- https://github.com/browser-use/browser-use/issues
- https://github.com/browser-use/browser-use/blob/main/pyproject.toml
- https://github.com/pydantic/pydantic-ai/issues
- https://github.com/pydantic/pydantic-ai/blob/main/pyproject.toml
- https://github.com/langchain-ai/langgraph/issues
- https://github.com/langchain-ai/langgraph/blob/main/Makefile
