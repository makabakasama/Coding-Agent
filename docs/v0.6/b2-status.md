# B2 实施记录

日期为 2026-11-11，北京时间。用户明确要求接着实施 B2。本页记录代码接入情况，运行验证尚未进行。B1 基础见 [B1 记录](b1-status.md)，整体设计见 [Chat 设计](chat-core-design.md)。

后续已完成指定离线验收，覆盖实际独立 Worker 终止/重启与保存结果恢复，最新结果见 [验收记录](chat-acceptance-20261111.md)。下文保留实施当时的范围与检查状态。

## 已接入的后端路径

```text
POST 会话轮次 → PostgreSQL chat_turn_jobs
                      ↓
            现有独立 Worker 共享线程池
                      ↓
            ChatExecution 读取历史与权限
                      ↓
            Provider.respond 生成文本
                      ↓
       材料落盘 → 回复/终态/事件/会话解锁事务
                      ↓
            GET 消息、轮次、事件
```

- `backend/repositories/chat_store.py` 增加领取、心跳、领取身份校验、上下文读取、事件写入、终态事务和中断轮次查询。条件更新与领取 ID 共同防止重复领取、旧领取者覆盖结果。
- `backend/services/chat_execution.py` 增加纯文本讨论执行，懒创建 Provider，每轮独立模型对象，复用现有模型配置和 profile。测试模型需通过独立的 chat_model_factory 显式注入，并标记 injected_test_model。
- `core/chat_context.py` 依据消息截止点组装已保存的跨轮消息，保留消息来源、顺序和指纹；历史失败附程序状态，当前轮必须以实际输入消息结束。
- `backend/services/worker_pool.py` 可选接收 ChatExecution，在聊天与原编码队列间交替尝试领取，两类工作共用原线程池容量、数据库会话锁和本机执行锁。
- `backend/worker.py` 正常启动入口已传入 ChatExecution。既有直接构造 WorkerPool 的调用方可继续仅处理编码队列。
- `core/provider.py` 在 respond 路径记录实际序列化请求、原始响应及每次 HTTP 尝试开始，继续保留结束/失败和用量事件。decide 路径沿用原事件行为。

纯文本 B2 每轮进行一次模型决策，使用 Provider 内有界传输重试。讨论不执行文件、测试、命令或其他工具，项目只读工具按 C1 接入。无工作区会话可进入相同讨论路径。

## 事务与状态

领取在事务内核对 queued、会话活动轮和 namespace，保存 worker_id/claim_id、running 与开始事件。模型网络等待期间不持有数据库事务。

完成时在同一事务保存 assistant 消息、responseMessageId、Turn 终态、Job 终态、终态事件、next_message_seq、会话 revision 和活动轮释放。重放完成操作不会再生成一条回复。结束后的新轮可携带上一轮实际保存的消息。

Provider、协议、上下文或材料错误形成失败终态和原因。故障记录与已有材料保留，结束后的用户可继续追问。消息与事件查询继续使用 B1 的增量接口。

心跳超过 15 秒时，Turn 查询和幂等提交重放返回 unknown/WORKER_HEARTBEAT_LOST 的存活投影，保留活动轮，避免因一次心跳过期直接重放工作。原领取仍存活时可恢复心跳并正常完成。

## 中断恢复

Worker 启动取得既有数据库会话锁和本机执行锁后，先处理本命名空间旧的 running/unknown 聊天轮次。

1. 查找原 conversation/turn/claim 对应的完整 result.json。
2. 核对记录身份、终态、回复和引用文件的路径及 SHA256。
3. 完整结果可恢复时，调用同一完成事务补齐回复与终态，标记 recovered。
4. 完整结果缺失时以 WORKER_INTERRUPTED 结束；结果损坏或材料校验失败时以 RECOVERY_EVIDENCE_INVALID 结束。保留原文件，释放会话供新轮讨论。

恢复期间不重新调用模型。只有 reply.json 而缺少完整 result.json 时，该轮作为中断失败保留。数据库提交出错且结果已保存时，异常会阻止 Worker 继续领取，后续启动从保存结果恢复。旧领取者的晚到结果保留为本地材料，身份校验阻止其写入失效轮次。

## 上下文、材料与用量

材料路径为 `runs/chat/<conversation-id>/<turn-id>/<claim-id>/`，增加 claim 层以保留领取身份及晚到材料。具体包括 context.json、Provider 实际 request.json/response.json、reply.json、trace.json 和完整 result.json。Turn 与 assistant 消息保存材料相对路径及哈希。

材料按现有密钥值脱敏逻辑保存，保留路径文本。引用中的 redacted 字段标记本次内容是否改变。messagesSha256/requestSha256 对应原输入，文件引用 SHA256 对应实际保存的脱敏材料，脱敏后无法据此声称精确恢复原始敏感文本。完整材料沿用 runs 忽略约定。

Worker 新增两个配置参数，默认值由设计草案落实。

| 参数 | 默认值 | 行为 |
| --- | --- | --- |
| --chat-max-input-bytes | 48000 | 检查消息序列与 Provider 实际序列化请求，超限为 CONTEXT_LIMIT，保留已形成材料 |
| --chat-max-output-tokens | 2048 | 传入兼容 Provider 的 max_tokens 输出限制 |

输入字节上限与模型 token 窗口分别具有各自含义。当前未引入统一 tokenizer 或自动模型窗口发现，实际模型窗口限制由 Provider 校验；应用保留超限/上游失败并形成终态。当前保留完整历史，超限后用户可新建会话，自动摘要和消息选择 UI 留待后续安排。

记录每轮模型决策数、HTTP 尝试数、排队及处理耗时、规范化输入/输出/缓存 token 和用量完整性。别名字段选择其一，缓存用量单列。失败尝试费用可能未知，usageComplete 只在单次请求且输入/输出用量完整时为 true。注入模型未提供传输事件时，其 HTTP 用量保持未确认。

## 核对边界与下一步

本轮进行人工代码阅读、八个相关源码文件的 AST 语法解析及差异格式核对。未运行 pytest、数据库、HTTP、Worker 实际进程、真实 Provider 或页面验收，尚未证明上述路径在运行中通过。B1 的数据库与接口运行验证缺口仍保留。

下一批 B3 接入 Chat 页面、上下文和材料展示及 EvalRun 索引，并建立开发样本记录/评分通路。项目读取与从讨论进入编码执行按 C 批推进。

本轮未提交、推送、启动或重启服务。历史材料保持本机留存，未进行删除或迁移。
