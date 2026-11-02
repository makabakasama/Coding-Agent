# M3 运行与材料说明

接口见 [执行契约](execution-api.md)，证据见 [M3 验收记录](m3-verification.md)。仅支持普通修复任务，worktree 不提供执行沙箱。

## 生产入口

```powershell
.\.venv\Scripts\python.exe -m repo_agent.task_http --port 4176 --workspace-root experiments/tasks
```

选择未占用端口，不接管 4173/4175/4177 或其他未知服务。旧进程不会热加载。默认是 configured_provider，读取现有本机 .env/环境；浏览器不传密钥。用户依次保存任务、单独准备、查询 canStart、主动确认启动。启动 POST 返回后后台执行，刷新仅 GET。真实模型由用户主动启动；本轮未运行。

旧三字段 projects.json 兼容。可选 execution 示例（解释器相对配置文件目录解析）：

```json
{"projects":[{"projectId":"sample","name":"本机可信项目","repo":"../../sample","execution":{"testPython":"../../sample-env/Scripts/python.exe","testTimeout":90,"testEnv":{"PYTHONUTF8":"1"}}}]}
```

未指定 testPython 时使用服务解释器；这不保证目标依赖存在。testEnv 允许项和固定测试设置见契约。不要将本机配置、路径或 .env 复制到公开材料。

## 受控离线联调

```powershell
.\.venv\Scripts\python.exe -m examples.offline_task_workbench --port 0
```

只创建自己的临时合成仓库，打印实际空闲端口与已保存任务 URL，不接受真实仓库参数、不构造 Provider。source=injected_test_model；页面分别点击准备和启动。FixtureModel 从 read_file 反馈取 snapshot_id，再 replace_lines，最后 finish；最终固定 pytest 由后端另行运行。服务退出不自动删除材料。重启此脚本会创建新夹具，不恢复旧夹具；同任务恢复测试由后端用例覆盖。

前端报告其快照编辑联调地址为 [63128 临时任务](http://127.0.0.1:63128/#/tasks/ce1644a8fd234ea39ec9b7542e0ba28a)，本后端未再次探测存活。后端此前启动的 50298 是旧 replace_text 夹具，63120 是快照夹具；均为当时加载版本，不代表最终源码已热加载。不要将旧夹具结果当成最终版本回归。

## 状态和材料

同数据目录全局一次一个执行，同任务永久一次性声明。未结束记录失去锁后为 unknown；旧全局 owner 未核实会阻止新任务。没有取消、恢复、解锁、重试或清理接口；先人工核实执行及可能的测试子进程，不能删声明强行再次调用模型。

`runs/tasks/task_<ID>/execution/` 保存 state/result、编号事件、Agent source 快照、脱敏 trace、运行配置、测试输出/JUnit 和补丁导出信息。失败、步数上限和部分落盘同样保留。测试日志过滤已知密钥，不能声称与进程原始字节一致。candidate.patch 只在完整提取且未检出已知密钥时保存准确字节，patch-export.json 记录 SHA256；拒绝导出时查看 patchExport/incomplete，副本保留待审查。网页 diff 始终是脱敏文本，不能据此重放。

最终固定检查报告实际退出码、超时及 JUnit 计数；全跳过不能通过，nullable 计数不能显示为零。模型 finish、最终检查、范围审查分别判断；始终 independentlyVerified=false。最终审查核验文件、HEAD、Git 身份及关键 Git 元数据，发现测试副作用则保留并报告，不能据此宣称隔离任意代码。
