# M2a 独立工作副本准备契约

日期：2026-10-31。M1 `task.json` 与三个接口保持不变。

## 接口和状态

- `GET /api/tasks/{id}/preparation`：200，返回准备记录；旧任务没有记录时返回 `not_prepared`。
- `POST /api/tasks/{id}/preparation`：JSON 请求体必须为 `{}`，沿用 M1 的同源、大小和错误约定。用户单独点击后调用；同步等待准备完成，首次及重复请求均返回 200 和当前准备记录。响应丢失后先 GET 查询。
- 返回公共字段：`schemaVersion: 1`、`taskId`、`status`。状态为 `not_prepared`、`preparing`、`prepared`、`preparation_failed`。
- 开始后增加 `preparationId`（32 位随机串）、`startedAt`、`baseCommit`、`workspaceId`（等于 preparationId）、`isExecutionSandbox: false`；结束后增加 `finishedAt`。失败增加 `error: {code, message}`。公共接口不返回本机路径。

任务保存状态仍为 `created`；准备状态独立，不代表修复或验收成功。客户端不得把 preparing 当作 Agent 执行中。

准备成功响应示例：

```json
{
  "schemaVersion": 1,
  "taskId": "11111111111111111111111111111111",
  "status": "prepared",
  "preparationId": "22222222222222222222222222222222",
  "workspaceId": "22222222222222222222222222222222",
  "startedAt": "2026-10-31T07:00:00Z",
  "baseCommit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "isExecutionSandbox": false,
  "finishedAt": "2026-10-31T07:00:02Z"
}
```

尚未准备只返回三个公共字段。声明已创建但首次记录尚未保存时，preparing 也可能只有三个字段；若此时中断，失败记录只有公共字段、finishedAt 和 error。前端不能假定 preparing/失败必有 workspaceId。

## 持久化和并发

任务目录下独占创建 `preparation/` 作为永久声明，原子写 `record.json`；本机路径和 Git 身份放在同目录私有 `identity.json`。副本固定在服务配置的 workspace 根目录下 `<projectId>/workspaces/task_<taskId>_<preparationId>_agent`。默认根目录为项目下 `experiments/tasks`。

同任务的 OS 文件锁串行化写入，跨线程及服务进程有效。锁被占用时 GET/POST 返回当前状态；声明与首条记录之间的短窗口返回 preparing。首次声明后禁止第二次创建；失败不自动重试，不覆盖、不清理失败副本。中断留下声明但无完整终态时，下一次无锁查询将记录 `PREPARATION_INTERRUPTED`。记录损坏返回 500 `PREPARATION_RECORD_CORRUPT`；存储故障返回 503 `STORAGE_UNAVAILABLE`。无法可靠保存失败时保留原材料并返回存储错误。

prepared 查询会核验路径、HEAD、独立 Git 目录及源仓库 common-dir；身份缺失或不匹配转为 preparation_failed / `WORKSPACE_IDENTITY_MISMATCH`，不自动修复。准备中的存活由 OS 锁判定，不依赖旧 PID 或旧状态。

## Git 和失败边界

只从任务已保存的完整 baseCommit 创建 detached worktree；不再解析 requestedRevision，不执行目标代码、模型、测试或依赖安装。使用现有 workspace.create_worktree，禁用仓库 hooks、已配置的 smudge/process 过滤器和递归子模块。过滤器不运行意味着如 Git LFS 文件会保留指针，prepared 不代表依赖或外部数据已准备完成。内部记录准确源目录、副本目录、HEAD、Git 目录、common-dir。Git 失败记录 `PREPARATION_FAILED`，材料全部保留，公共错误不含底层异常或路径。

未知任务、非法 ID 沿用 M1 错误；请求体非空或类型错误返回 400 `VALIDATION_ERROR`。没有任意文件读取、清理或重试接口。锁依赖本地文件系统，不支持网络共享、多主机服务。此批不改造既有 G/R/P 实验流程。
