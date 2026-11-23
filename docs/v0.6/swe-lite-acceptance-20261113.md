# D2a SWE Lite 定向离线验收

日期：2026-11-13（北京时间）。用户明确要求“进行验收吧”。范围仅为适配器离线回归和 CLI 帮助入口，不启动 Docker、真实 Provider 或正式 benchmark。

## 结果

- 解释器：现有 coding-agent Conda，Python 3.12.14。
- 最终专项：`tests/test_swebench_adapter.py` **11 passed，8.71 秒**。
- CLI：主入口、prepare、run-agent、grade、import-grade 的 `--help` 共 5 项，均退出 0；未据此推断实际环境执行成功。
- 生产源码未修改；修正测试夹具的补丁读取方式，并增加脚本模型完整链路及桩评分进程回传两项离线检查。

覆盖独立副本不改源仓库、gold/test_patch 不进入模型输入、重复任务拒绝、补丁包含新文件、保护测试配置、基线不足时不启动模型、累计预算在调用前拒绝、预测篡改和错误补丁拒绝、严格布尔字段、设施失败记 unknown、重评分追加不覆盖。

补充链路使用脚本模型驱动真实 Agent 与 TaskRepoTools，执行读取、快照编辑、finish，核对补丁、用量和源码隔离；禁用网络调用，并注入 TokenBudget 夹具。官方进程路径使用 subprocess 桩，验证固定 5.0.2 的 `logs/run_evaluation` 回传与导入流程。它们都是工程检查，不是实际模型表现或官方容器成绩。

## 首轮失败及修正

1. 首次启动命令的 PowerShell JUnit 参数拼接有误，pytest 退出 4、未执行测试；原日志保留。
2. 修正启动命令后：8 passed、1 failed。测试夹具使用 `read_text()` 读取包含 CRLF 的补丁，通用换行转换导致 JSONL 预测与原始补丁不同；生产路径本来使用 bytes 解码，拒绝不一致的行为正确。
3. 诊断确认原补丁含 3 个 CRLF，夹具预测含 0 个，统一换行后相等。夹具改为 `read_bytes().decode('utf-8')`，保留原始字节；同时补两项缺失链路检查，最终 11 passed。没有放宽生产补丁核对条件。

源码指纹对照确认验收期间仅 `tests/test_swebench_adapter.py` 变化；其他被核对的适配器及 source archive 实现未变化。

## 本机证据

- [完整摘要](../../runs/engineering/swe-lite-acceptance-20261113-141502/summary.md)
- [首次启动失败](../../runs/engineering/swe-lite-acceptance-20261113-141502/pytest-first.log)
- [9 项首次实际执行](../../runs/engineering/swe-lite-acceptance-20261113-141502/pytest-second.log)
- [最终 11 项输出](../../runs/engineering/swe-lite-acceptance-20261113-141502/pytest-third.log) / [JUnit](../../runs/engineering/swe-lite-acceptance-20261113-141502/pytest-third.xml)
- [解释器、源码指纹与 CLI 结果](../../runs/engineering/swe-lite-acceptance-20261113-141502/verification.json)

证据在被 Git 忽略的 runs 内，本机留存不表示远端备份。

## 验收边界与下一步

本次通过 D2a 适配器的指定离线检查。未运行全项目回归、官方任务环境、G+R 独立基线、实际官方评分器、Docker 或真实模型，也不产生模型任务成功率。

下一步准备 1 道预先固定的 SWE Lite 官方任务、数据来源、仓库基线和镜像资源；再按明确授权完成基线与实际官方环境核对。后续模型调用仍需单独确定预算。维持固定小子集、并发 1，不扩展为全量评测。
