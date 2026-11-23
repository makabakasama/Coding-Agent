# B1 实施记录

日期为 2026-11-11，北京时间。用户明确授权按 [Chat 设计](chat-core-design.md) 实施 B1。本页记录已接入的代码与验证边界。

后续已按用户明确授权完成指定离线验收，最新结果见 [验收记录](chat-acceptance-20261111.md)。下文保留实施当时的静态核对与范围记录。

## 已接入范围

| 范围 | 实现入口与行为 |
| --- | --- |
| 请求契约 | `backend/schemas/chat_contract.py` 校验会话、讨论轮次、ID、消息长度、revision 与分页参数；冲突响应附当前 revision 和活动轮 ID |
| PostgreSQL 仓储 | `backend/repositories/chat_store.py` 复用现有数据库连接池，初始化会话、消息、轮次、Job 和事件五张表及索引 |
| 原子提交 | 会话行锁内检查幂等键，再检查 revision/活动轮，保存用户消息、轮次、队列记录、首条事件与更新后的会话，事务统一提交/回滚 |
| 查询与隔离 | 按数据目录命名空间读取会话，按会话核对轮次归属；消息/事件按序号增量分页，会话列表按更新时间和 ID 游标分页 |
| 服务与 HTTP | `backend/services/chat_service.py`、`backend/routers/chat_http.py` 通过现有 TaskService/TaskHandler 组装，复用来源校验、JSON 读取与错误处理 |
| Provider | `core/provider.py` 新增 TextReply、ToolCallReply 和 respond()，共用传输、重试与用量；既有 decide() 继续输出 Decision |

会话工作区可空，绑定时须引用已经登记的项目。当前 discuss 权限快照固定为禁止写入、禁止测试、无工具，项目读取工具在 C1 接入。execute 请求在本阶段明确拒绝。

幂等键为 conversationId + clientRequestId，同键相同语义内容返回原 Turn，同键异内容返回 REQUEST_KEY_REUSED。expectedRevision 是并发前提，排除于语义摘要之外，幂等重放先于 revision 和活动轮校验。新轮返回 202，重放返回 200。

会话创建返回 201。conversationId/turnId 使用 32 位小写十六进制 UUID，clientRequestId 接受 1 到 128 位字母、数字、下划线或连字符。用户消息上限 8000 字符。模式默认 discuss。

## HTTP 入口

以下接口已接入现有本机 HTTP 分发。

- POST /api/conversations
- GET /api/conversations?cursor=…&limit=50
- GET /api/conversations/{id}
- GET /api/conversations/{id}/messages?afterSeq=0&limit=50
- POST /api/conversations/{id}/turns
- GET /api/conversations/{id}/turns/{turnId}
- GET /api/conversations/{id}/turns/{turnId}/events?after=0&limit=50

这些入口依赖 PostgreSQL 存储。已有文件存储调用者继续使用原任务能力，访问 Chat 返回 CHAT_UNAVAILABLE。会话查询使用数据库实时状态，当前未增加 Redis 缓存。

respond(messages, tools=None, response_limits=None) 默认纯文本请求，未显式提供工具时不发送工具定义。传入工具时仅返回经过现有原生决策校验且本次确实提供过的工具。它只解析响应，工具执行仍由调用方负责。空回复、截断、内容过滤和非法工具响应会产生明确异常。

response_limits 支持正整数 max_tokens 与 max_input_bytes，respond 默认请求上限 48000 字节，按实际序列化后的 HTTP 请求体计算，调用方可显式调整。该值是应用请求大小限制，模型 token 窗口及输出预留需在 B2 的配置与上下文组装中接入。

## 静态核对与尚未验证项

已人工阅读事务、路由、命名空间、错误分支和 Provider 共用传输逻辑，七个相关源码文件通过 AST 语法解析，git diff --check 通过。静态语法检查只读取源码，未导入产品模块。

本轮遵循项目测试约定，未运行 pytest、数据库集成、HTTP 请求、真实模型或服务启动。新表 DDL、并发原子性、分页、错误分支及旧 Provider 调用兼容性尚待运行验证。既有历史测试数字不作为本轮通过证据。

B1 当时接通提交和查询基础，Worker 消费与回复属于下一批范围。后续用户已授权实施 B2，代码接入与尚未验证项见 [B2 记录](b2-status.md)，页面与评估记录仍按 B3 推进。

本轮未提交或推送。产品代码之外的既有本地改动保留，未执行历史材料迁移或清理。
