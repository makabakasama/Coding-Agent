# PostgreSQL 任务记录

安装依赖后，在项目根目录 `.env` 中配置连接地址。

```powershell
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/coding_agent
```

数据库需提前创建。连接用户需具有建表和读写权限，首次启动自动创建 `tasks`、`task_runs` 及 `task_dispatch` 等表。密码中的保留字符需进行 URL 编码。环境变量 `DATABASE_URL` 优先于 `.env`。API 与独立 Worker 均要求该配置，启动方式见 [Worker 池](worker-pool.md)。

按原来的命令启动网页服务。

```powershell
.venv/Scripts/python.exe -m repo_agent.task_http
```

配置连接地址后，新需求及第一轮记录在同一个 PostgreSQL 事务中提交。新增轮次通过外键关联原需求。`tasks.record` 保存需求 JSON，`task_runs.record` 保存轮次任务 JSON，`metadata` 保存目录关联信息。数据库同时由 task_dispatch 保存调度状态、领取与心跳，task_trash 保存回收站状态；细粒度执行事件、结果、Diff 和撤回材料仍由文件提供。

数据库提交后生成现有目录结构中的 JSON 文件。文件写入失败时返回包含任务 ID 的错误，数据库记录保留。读取该任务或其轮次列表时会重试生成缺失文件。提交结果不明确时，先查询任务列表确认已保存记录，再决定是否重新提交。

任务详情、需求列表、轮次列表和最近任务列表会读取数据库，并兼容原有 JSON 记录。已有文件保持原样。旧记录可通过历史导入纳入数据库。普通执行入口将本轮任务写入持久队列，由独立 Worker 领取执行。

离线检查可运行以下命令。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_database_task_store.py tests/test_tasks.py tests/test_project_task_store.py tests/test_config.py -q
```

真实数据库测试单独读取环境变量 `CODING_AGENT_TEST_DATABASE_URL`，应指向专用测试库。测试会建表、写入随机命名需求、验证服务对象重建后的读取和文件恢复，并在结束时删除本次数据库记录。未配置时跳过该项。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_database_task_store.py -q
```

## 导入已有记录

配置 `DATABASE_URL` 后运行历史导入命令。

```powershell
.venv/Scripts/python.exe -m repo_agent.import_task_history
```

导入在一个事务中完成。普通需求与轮次保留原编号及实际目录，旧格式轮次的原始元数据保持原样。登记实验的需求、轮次及验收视图保存到 `experiment_tasks`、`experiment_rounds`。报告、日志、补丁和需求初始材料的原始字节、相对路径与 SHA256 保存到 `historical_materials`。`experiment_locations` 登记环境和工作副本位置，源码副本、解释器和依赖环境继续保存在文件系统中。

重复导入会核对原始记录与材料，一致时跳过，冲突时回滚本次导入。`experiment_rounds` 是从已核对材料重新生成的页面视图，允许随读取逻辑更新。原文件保留。实验历史报告和已登记附件优先读取数据库快照，未导入材料继续从文件读取，附件公开读取沿用现有脱敏与分页流程。旧格式 metadata、baseline 检查、执行前差异和控制台日志单独展示，原来缺失的标准报告仍显示缺失。任务运行后产生的新日志暂由现有执行流程保存，历史导入是显式操作。
