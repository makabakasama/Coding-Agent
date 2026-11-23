# Execution Receipt First

版本 `v0.6-execution-receipt.2`。独立快照及原 RC1、P2 的提交和 bundle 校验见本机归档的 `version-final.json`。

## 执行和证据

本机和容器使用同一执行回执和解释函数。回执保存实际 argv、工作目录、退出码、执行阶段、超时与中断、耗时、源码和环境标识、日志路径及完整性。本机保存两路日志，容器保存合并输出及完整归档引用。证据读取采用本次执行生成的 JUnit、可识别文本、执行回执的顺序。日志出错、报告冲突和用例证据缺失都有独立状态。

以下为旧版 pytest 产物重放的有效字段节选。

```json
{
  "execution": {
    "status": "exited",
    "stage": "tests",
    "exit_code": 0,
    "timed_out": false,
    "logs": {"status": "complete"}
  },
  "check_status": "unknown",
  "test_evidence": {"source": "execution", "granularity": "none", "report_status": "not_requested"}
}
```

套件计数与命名用例结果分开保存。需要确认逐项需求时，继续使用现有测试版本、范围、断言映射和 Oracle 检查。只有可识别输出的归因会用于断言和回归结论。官方评分导入另行校验必需用例的判分证据。存在缺失或矛盾时保留原报告，并在本项目登记 `unscored`。

## 接入已知测试入口

调用方可根据公开仓库配置或已准备的执行环境指定 argv。普通 CLI 使用 `--test-argv`，Evaluation 开发检查使用 `--check-argv`。值为 JSON 字符串数组，其中 `{target}` 是一个独立参数。运行器支持 JUnit 时可将独立的 `{report}` 参数交给该入口。现有目标授权、工作区限制和测试超时继续生效。

## 验收证据

- 10 题官方产物离线重放，9 份基线继续有效，Sphinx 8627 仍缺 17 条控制用例结果，原始报告的字节保持一致。
- 四份开发检查产物重放，pytest 5221 两组保留退出码 0，Flask 4045 两组保留启动阶段 `INFRA_ERROR`。
- pytest 5221 同一公开测试目标在现有镜像上完成一轮无模型检查，JUnit 记录 73 项通过、1 项跳过。检查后容器已清理。
- P0、P1、P2.2 和相关入口回归共 139 项、27 个子检查通过。16 项依赖专用数据库的 Chat 集成检查跳过。
- 验收日志和完整回执保存在本机 `runs/engineering/execution-receipt-da1e0a22/`。

工程验收 PASS。正式 Benchmark Preflight 仍为 FAIL。Sphinx 官方逐项输出缺失，Flask 的原开发检查环境错误未处理。
