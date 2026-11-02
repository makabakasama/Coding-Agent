# M2a 交付与验证

日期：2026-10-31。范围仅为独立工作副本准备；接口权威说明见 [准备契约](preparation-api.md)，启动与恢复说明见 [后端运行说明](m2a-runbook.md)。

## 实现

新增 `task_preparation.py`；任务服务与 HTTP 层增加 GET/POST `/api/tasks/{id}/preparation`。POST 空对象，返回 200 和当前准备状态；状态独立于 M1 created：not_prepared、preparing、prepared、preparation_failed。OS 文件锁和永久准备声明防止重复创建；重启后无锁的未完成声明转为失败。副本从 baseCommit 创建，记录并复核 HEAD、detached 状态、独立 gitDir 和源 commonDir。

M1 task.json 未修改，旧任务可查询并主动准备。私有身份记录保存准确路径，公共接口不公开本机路径，无任意文件下载。失败声明和部分副本不清理；没有重试创建、取消或执行接口。创建时禁用 hooks、checkout 过滤器和子模块递归；依赖与 LFS 内容没有准备。worktree 不是沙箱。

## 后端会话直接执行的检查

`.venv/Scripts/python.exe -m pytest tests/test_tasks.py tests/test_task_preparation.py tests/test_workspace.py -q`：最终 **29 passed，58 subtests passed，12.64 秒，退出码 0**。

其中 M1 为 15 项，M2a 新增 10 项，既有 workspace 为 4 项。覆盖合成仓库上的 commit 固定、移动源 HEAD 后准备、M1 不变、服务重建、重复请求、两个服务对象并发、跨进程持锁与释放、中断声明、部分失败保留、存储错误、损坏记录、工作副本身份失配、禁用 hooks/过滤器，以及真实回环 HTTP 成功与错误响应。

开发中发现 Windows 初始化锁文件时读取已锁字节会失败，已移除初始化读写并验证。首轮过滤器测试在构造 fixture 时过早启用 required，导致 git add 失败；已调整为先保存合成 baseline 再配置过滤器。失败输出保留，没有当成功证据。

保持最终源码和测试不变后，运行 `.venv/Scripts/python.exe -m unittest discover -s tests -v`：**166 项通过，396.771 秒，退出码 0**。这是全项目离线回归，不能推导为真实模型或目标项目验收。

本机输出目录为 `runs/engineering/m2a_20261031_backend/`：`specialized-delivery.txt` 为最终专项；更早 specialized 文件为迭代记录。`full-final.stdout.txt`、`full-final.stderr.txt`、`full-final.exit-code.txt` 为最终完整回归；`source-hashes.json` 保存后端源码、测试和契约哈希。材料被 Git 忽略，不代表已提交或备份。

首轮完整回归为 166 项、2 errors、401.876 秒，保存在 `full.*`：该进程启动后仍在修正测试夹具和锁初始化，进程内旧代码与子进程读取的新代码不一致。错误分别是旧过滤器夹具在 git add 时失败、旧锁初始化访问子进程已锁字节。该轮不作为最终验收；修正后专项通过，并在源码保持不变后重新完成上述全量回归。没有删除旧输出。

## 前端交付及联调结果（前端会话报告）

来源：前端会话交付报告，经主会话转交；可对照 `frontend/README.md` 的“M2a 已接入”及“前端验证”段落。本节是来源明确的结果整合，不是后端会话再次执行浏览器、HTTP 或 Node 检查所得。

前端报告 `node --check frontend/app.js` 通过，`node --test frontend/tests/task-ui.test.cjs` **13 项通过**（M1 8 项、M2a 5 项）。新增模拟检查覆盖显式空 JSON POST、防重复、响应丢失后 GET 恢复、稀疏 preparing/中断失败、损坏/commit 不匹配及导航旧响应保护。

前端使用新端口 4177 和独立临时合成 Git 仓库、项目配置、任务目录、副本目录，通过实际同源服务完成以下检查：

| 证据类型 | 前端报告结果 |
| --- | --- |
| 浏览器准备成功 | 从 not_prepared 单独点击后展示 prepared；Agent 启动保持禁用 |
| 浏览器刷新与手动查询 | 恢复同一 prepared 和副本标识，不再次发送准备操作 |
| 浏览器中断声明夹具 | 人工仅预建无记录声明的临时任务显示 preparation_failed / PREPARATION_INTERRUPTED，缺失字段显示未提供，准备禁用；这是故障夹具，不是自然发生的真实事故 |
| 实际 HTTP 重复 POST | 返回同一 preparationId；磁盘仅有一份副本 |
| 实际副本版本核对 | HEAD 等于任务固定 baseCommit |

成功任务 `29dca745b1ae4738b629cb7d8bc780d3`，准备编号 `346b34197ca643ee945c199fad44ca03`，合成 commit `7a471aa7531c81df011c8d623bb56259a08e55b0`；中断声明夹具任务 `e27f9effc12c428fad6e0fde576025e4`。前端报告截图为其会话 visualizations 目录下 `m2a-prepared.png`、`m2a-failed.png`，本后端收尾未检查或复制这些截图。

前端报告交付时临时 4177 服务仍运行，成功详情为 [临时合成任务](http://127.0.0.1:4177/#/tasks/29dca745b1ae4738b629cb7d8bc780d3)。本后端收尾未探测其当前存活情况；4173/4175 旧服务未动，不能据此说 4175 已升级。准备中、网络断开、损坏及旧响应只做模拟验证；跨标签页和服务重启未做浏览器验证。未运行真实模型、目标项目测试或依赖安装。

## 后端收尾契约审阅

本后端会话只读审阅 `frontend/app.js` 的 preparationPanel、taskDetail 及对应模拟用例，与 `preparation-api.md` 对照：GET/POST 路径、POST `{}`、四类状态、独立准备按钮、权威 not_prepared 才启用、未知结果不重发、稀疏字段、prepared commit 匹配、导航旧响应保护及禁用 Agent 均一致。未发现必须修正的实质契约偏差；本次没有修改 frontend/、重跑测试或操作服务。代码审阅不代替新增浏览器实测。

## 未完成及边界

- 本后端会话未修改 frontend/，未直接做 M2a 浏览器验收；前端已报告完成上述对接及合成联调，边界与来源见上文。
- 未重启现存 4175 进程，也未停止 4173 未知服务；旧进程不会自动加载代码。
- 未运行真实模型、目标项目测试、依赖安装或既有真实实验；离线合成检查不等于真实修复验收。
- 未提交、推送或发布；保留已有 `.gitignore`、README 和 M1 成果。
- 文件锁限本机文件系统；不承诺断电级持久性、抵抗同机恶意篡改或执行隔离。身份检查核对副本归属与 HEAD，不宣称测试依赖或工作文件已经验收。
