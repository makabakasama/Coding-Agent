# 正式评分入口真实容器验收

日期：2026-11-13。用户明确要求直接执行真实验收。本轮通过现有 coding-agent Conda 调用生产 execute_grader，真实启动 Docker 和官方 swebench 5.0.2，不调用模型。

**结论：本题正式评分入口按修复前基线标准验收通过。** 目标 test_trace 失败 1 项，120 项控制全部通过，官方 resolved=false、infra_failure=false，进程退出 0，逐题报告成功导入。该 fail 是未修复源码的预期结果，不是 Agent 的失败或修复成绩。

源码版本不符的分支实际触发：专用镜像恢复固定 G/tree 后核验通过，仅替换评分输入副本的 image 字段；脚本和补丁 CRLF 数均为 0，实际补丁字节与候选一致，源与验收副本干净。模型调用 0。

完整模块仍为 120 passed、2 failed、2 expected to fail、2 exceptions。另一个未计分失败 test_print_basic，以及 test_issue_8470/test_issue_13559 的异常均保留，不宣称整文件全部正常。离线异常分支回归本轮未运行，本次单题结果不能覆盖所有错误路径。

## 运行记录

- 成功轮：`run_20261113-181845_5aeb6fe6`。
- 评分轮：`verify_20261113-181906_1e0ee4b5`。
- [验收核对](../../runs/evaluation/swe-bench/run_20261113-181845_5aeb6fe6/acceptance-verification.json)
- [官方逐题报告](../../runs/evaluation/swe-bench/run_20261113-181845_5aeb6fe6/official/verify_20261113-181906_1e0ee4b5/logs/run_evaluation/verify_20261113-181906_1e0ee4b5/coding-agent/sympy__sympy-15308/report.json)
- [导入成绩](../../runs/evaluation/swe-bench/run_20261113-181845_5aeb6fe6/grades/grade_20261113-182202_0a177339/grade.json)

实际通过 grade 使用的 execute_grader 完成调用与导入；为复现基线而创建的 attempt 明确标为 not_run_baseline_identity，未伪装为模型运行。本批没有修改评分实现。

首次在沙箱中执行时 Docker 引擎未启动，失败轮 `run_20261113-181605_d07885b8` 保留，未测试、unknown。随后在用户环境确认引擎不存在并启动 Docker Desktop，通过进程级 DOCKER_HOST 指向 desktop-linux 管道，新建成功轮。源目录因 Windows 所有者不同，已按授权将该具体路径纳入用户 Git safe.directory；未设通配信任。镜像与探测容器保留，官方评分容器由 harness 管理；未清理历史材料。

下一步为确定模型与单题 token/时间预算，明确授权后运行 Agent、保存实际补丁，再做官方评分。现无真实模型成绩，也不代表全量 benchmark 成功率。本机忽略目录不代表远端备份。
