# Redis 读取缓存

在 `.env` 中设置 `REDIS_URL` 启用缓存。

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
```

API 和 Worker 共用同一配置。数据库地址与本机任务目录共同生成缓存命名空间，区分不同数据库和测试工作区。Redis 连接地址及凭据不会写入缓存键。

| 数据 | 缓存时间 | 更新规则 |
| --- | --- | --- |
| 固定任务记录、轮次位置、原需求约定 | 300 秒 | 创建及历史导入后清理关联轮次键 |
| 已导入历史报告和附件字节 | 300 秒 | 原始材料导入要求内容一致 |
| 页面查询的调度状态 | 1 秒 | 入队、领取、心跳、终止和中断确认后清理 |
| 普通任务列表、回收站列表整份响应 | 静态列表 30 秒，有排队、执行或整理中的任务时 1 秒 | 创建、导入、删除、恢复、入队、领取、心跳及终止后更新版本键 |
| 项目列表、需求列表、轮次统计 | 实时读取 | 批量查询 PostgreSQL |
| 领取、入队前确认、撤回互斥 | 实时读取 | 使用 PostgreSQL 事务与锁 |

缓存采用读取穿透方式，先查询 Redis，未命中再读取 PostgreSQL 并填充。不存在的记录不做负缓存，新增数据可以立即被查询。调度状态的页面查询最多允许约 1 秒缓存延迟。任务列表命中整份响应缓存时直接返回，无需读取 PostgreSQL 或逐项扫描运行材料。未命中时批量查询调度状态并读取材料生成列表。需求列表通过批量统计获得轮次数。

列表使用版本键隔离缓存。写入提交后切换版本，并发读取即使较晚完成也只写入旧版本，避免覆盖新版本结果。Redis 故障期间回退数据库，故障造成失效通知丢失时，其他进程的旧列表仍受上述缓存时间限制。关键操作继续在数据库实时校验。

Redis 连接和读取超时各为 150 毫秒，关闭自动网络重试。连接失败后本进程暂避 5 秒，直接使用数据库。缓存故障不改变任务持久化、领取或撤回操作的数据库语义。任务的执行结果文件和撤回状态仍实时读取。

Redis 可使用本机服务或容器。以下检查命令假设你已自行配置名为 `coding-agent-redis` 的容器；仓库不会自动创建该容器。Redis 缓存可丢弃，持久记录由 PostgreSQL 与原始材料目录提供。

检查容器可使用以下命令。

```powershell
docker ps --filter name=coding-agent-redis
docker exec coding-agent-redis redis-cli ping
```

开发服务使用 `--reload` 时会检测 `.env` 和源码变化并重载。其他启动方式需重启 API 和 Worker 加载缓存配置。

`tests/test_redis_cache.py` 覆盖命中、过期、失效、损坏 JSON、空附件、故障回退及无负缓存。配置 `CODING_AGENT_TEST_DATABASE_URL` 与 `CODING_AGENT_TEST_REDIS_URL` 后，`tests/test_task_queue.py` 还会检查真实 PostgreSQL/Redis 下的查询次数、跨实例状态失效和独立 Worker 执行。


## PostgreSQL 连接池

每个 `DatabaseTaskStore` 使用 `psycopg_pool.ConnectionPool`。正常启动时 API 进程与独立 Worker 进程各持有一个池，池内线程共享连接。每个池按需建立连接，`min_size=0`、`max_size=6`，获取连接超时 5 秒，连接建立超时 5 秒。两进程各一个池时最多共 12 条连接，Worker 的长期调度会话占其池的一条连接。

`_connection()` 从池借用连接，成功提交，异常回滚，随后归还池。Worker 的长期 advisory lock 会话使用 autocommit，归还时重置为手动事务模式，防止其他请求失去事务边界。API 关闭、Worker 退出、历史导入结束及初始化失败时关闭连接池。依赖已加入 `requirements.txt`。

`tests/test_database_task_store.py` 验证连接复用、异常回滚、autocommit 重置、池耗尽超时与关闭。`tests/test_task_queue.py` 验证列表缓存命中时无数据库连接借用或材料读取，以及不同服务实例之间的失效传播。
