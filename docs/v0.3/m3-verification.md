# M3 Agent 接入交付与验证

日期：2026-10-31。状态：本批后端实现、最终专项及全项目离线回归完成；前端报告完成合成离线闭环。契约见 [execution-api.md](execution-api.md)，运行见 [m3-runbook.md](m3-runbook.md)。真实模型尚未验证。

## 实现范围

task_execution.py 后台复用 Agent/Provider 循环；task_execution_tools.py 对现有 RepoTools 增加普通任务编辑范围、受保护测试/配置和冻结目标约束。HTTP 提供执行启动/查询、游标事件、结果、受控材料分页。task_projects 支持兼容旧配置的可选本机执行设置。tools.py 的 JUnit/输出过滤均为可选参数，既有 G/R/P 入口未改造。

启动核验固定 commit、登记副本身份和干净状态；OS 锁与永久声明防止并发/重复执行。失去执行锁的未结束记录保守显示 unknown，禁止自动重跑。最终独立运行冻结检查并保存实际计数、输出及审查；测试副作用、异常、步数上限、材料失败均保留。模型 finish 不推导检查通过或独立验收。

补丁无已知密钥且提取完整时准确保存并计算 SHA256；检出已知密钥或无法完整提取时拒绝原始导出并说明，网页只给脱敏 diff。stdout/stderr/JUnit/trace 是过滤已知密钥后的记录，不承诺原始字节一致。

## 后端直接验证

最终专项命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_task_execution.py tests/test_task_preparation.py tests/test_tasks.py tests/test_test_logs.py tests/test_tools.py tests/test_workspace.py -q
```

**61 passed，75 subtests passed，94.46 秒**。输出：`runs/engineering/m3_backend_20261031/specialized-final.txt`。其中 M3 12 项覆盖：

- read_file → snapshot_id → replace_lines → run_tests → finish 的现有循环，另行固定最终检查；真实回环 HTTP、事件分页/实际反馈、材料分页、重复启动与服务对象重建读取。
- 异步启动、全局互斥、未准备/脏副本/身份/配置拒绝、无效 HTTP 输入、永久声明与未知旧 owner。
- 测试/范围编辑拒绝、模型改目标拒绝、未授权测试、全跳过/混合计数、无收集/收集错误及不可解析 JUnit 不判通过。
- step_limit、模型/工厂异常、result 落盘失败保留、测试副作用越界并停止剩余检查、已知密钥拒绝原始补丁/脱敏 trace。
- candidate.patch 文件真实 SHA256 与公开 patchExport 一致，未提供原始补丁下载；独立验收标志为 false。

首次专项 53 项通过属于较早实现，不作为最终证据；扩展 M3 单测 12 项/2 subtests 通过属于中间检查。早期一次8项执行检查因测试助手重复传 checkTargets 而有1失败，后修正并纳入最终检查。已有输出保留。

源码保持不变后运行 `.venv/Scripts/python.exe -m unittest discover -s tests -v`：**178 项通过，466.456 秒，退出码 0**。完整输出为同目录 `full-final.stdout.txt`、`full-final.stderr.txt`、`full-final.exit-code.txt`；`final-source-hashes.json` 保存最终后端源码/测试 SHA256，`verification.json` 汇总结果。该回归包含既有实验编排的离线合成用例，不代表重新执行两个真实仓库实验。

## 前端提供的证据

来源为前端会话交付报告与当前 `frontend/README.md` 的 M3 验证段落。本后端只读核对并整合，没有重跑浏览器或 Node 检查。

前端报告语法检查及 **25 项模拟行为检查通过**。实际浏览器使用独立临时合成服务 [63128](http://127.0.0.1:63128/#/tasks/ce1644a8fd234ea39ec9b7542e0ba28a)：分开准备/启动，来源标 injected_test_model；实际 read_file → replace_lines → finish，3/5 决策步；固定最终检查1通过、0失败/错误/跳过，范围审查另列通过；读取脱敏 diff 与 stdout；刷新恢复同执行，重复 HTTP POST 返回原 executionId，磁盘仅一个声明。

任务 `ce1644a8fd234ea39ec9b7542e0ba28a`；执行 `de8a24f73fb345a385b5cd7dd0f3998a`；合成 commit `f9a0e386811dcbad78f5ab60d24b227b9a01e23f`。前端报告补丁157字节、SHA256 `35dc711418e60edc7974186bd853c7ab08db843f16e43066417b07a53d8566ba`。截图为其会话 m3-result.png/m3-full.png，本后端没有复制或重新查看。

前端旧 replace_text 自管服务已停止且材料保留；该轮只属旧工具路径，不计快照证据。63128 也只证明其启动时版本，最终后端源码另由专项/回归验证。网络失败、全跳过/未知计数、材料截断/多页、跨标签页和崩溃没有逐项浏览器故障注入，只按模拟或后端检查记录。

## 边界

未调用真实模型，未使用/改写真实实验副本或历史材料，未安装依赖；仅临时合成仓库测试。前端由前端会话维护。没有提交、推送、发布或停止未知服务。runs/experiments 和临时材料不是远端备份。worktree 不是执行沙箱；测试可能有外部副作用，已知密钥过滤不是任意内容的完整秘密检测。没有执行取消/恢复接口，unknown 需要人工核实。
