# SWE Lite 单题修复前基线验收

日期：2026-11-13（北京时间）。用户明确授权安装固定官方依赖、拉取单题镜像并执行目标与控制测试；不调用真实模型。

## 结论

**冻结的修复前基线成立：目标 `test_trace` 失败 1 项，120 项控制测试全部通过。** 目标失败为 `latex(trace(A))` 的断言差异，非依赖或导入失败。官方评分 `resolved=false`、`infra_failure=false`，属于修复前预期结果，不是 Agent 修复成功。评分进程退出 0，模型调用 0，Agent 尚未启动。

整份测试文件原始结果为 `120 passed, 2 failed, 2 expected to fail, 2 exceptions`。除目标外，未计分失败为 `test_print_basic`，未计分异常为 `test_issue_8470` 与 `test_issue_13559`（旧 Sympy AST 对 False 常量的解析异常）。这些不在冻结评分清单中，完整输出保留；不能表述为整个测试文件全部正常。

## 固定身份与环境

| 项目 | 实际记录 |
| --- | --- |
| 任务 | `sympy__sympy-15308`，SWE-bench Lite test |
| 数据 revision | `b0dde1093fe417d83b7184254edf8199c1f0dff5` |
| 源码 G | `fb59d703e6863ed803c98177b59197b5513332e9` |
| 源树 | `e1ae244c37061fe44a05e7548185057ec8bbe09e` |
| 评分器 | 官方 `swebench==5.0.2`，安装于现有 coding-agent Conda |
| 宿主 / 目标解释器 | Python 3.12.14 / 容器 testbed Python 3.9.20 |
| 导入来源 | 容器 `/testbed/sympy/__init__.py` |
| 并发 / 测试超时 | 1 / 600 秒 |
| 官方汇总 | completed 1、unresolved 1、resolved 0、infra failures 0、errors 0 |

固定 revision 的 Parquet 下载后仅提取本题核对，全部列与之前 rows API 冻结导出一致。Parquet 为 2,702,170 字节，SHA256 `438e281d80587aa7be470896ce410557002fde02d2ceee3e099331d308f62dd3`；来源边界已补核，不改写原 preparation.json 的历史记录。

依赖安装前后已有包版本没有改变；新增评分依赖后 `pip check` 无缺失。源工作副本与待用 Agent 副本均干净。使用删除/加入相同行的 identity patch 触发官方执行，未留下业务修改；未使用参考修复补丁。

## 本次必要适配

1. 拉取的官方题目镜像默认 HEAD 为 `c9e2fec9793acf1c508ac03f593349c74a088bdb`，与题目 G 不同。官方脚本只恢复测试文件，不能自动保证业务源码正确。以固定镜像 digest 为基础，在专用派生镜像中恢复 G，并核对 HEAD、tree、工作区和导入位置后执行。
2. Windows 默认文本写入会将脚本/补丁转为 CRLF。本次使用运行目录中的启动包装，仅对 `eval.sh` 和 `patch.diff` 保留 LF；安装的官方包与评分逻辑未修改。

原镜像 linux/amd64 digest 为 `sha256:13edc0fe53d44216af865ac94a327dc899f01555f440ef4abb9951b986c283a1`；实际派生镜像 ID 为 `sha256:3a804433ade1173cc55515738884d797ac090eacb078abd4e3f02557f55ba0cb`。原镜像压缩约 1.07GB，展开约 3.96GB。

实际 eval.sh 与原数据脚本有两行差异：官方 5.0.2 自动插入测试退出码采集与输出。移除这两行后逐字一致；测试命令与 R 未改。实际脚本和补丁 CRLF 均为 0，补丁与预测输入逐字一致。

**这些是本次运行的显式环境适配，尚未接入生产 `grade` 入口。** 本次证明本题 G+R 能按官方评分规则建立基线，不能宣称默认评分入口已经完整验收。

后续实施更新：正式评分入口现已接入上述两项处理，新增实现尚未测试，见 [实施记录](swe-grader-adaptation-20261113.md)。本报告仍只记录此前基线运行事实。

## 材料与下一步

本机记录：`runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/baseline_20261113-152614_1dcd2fd7/`。

- [机器核验](../../runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/baseline_20261113-152614_1dcd2fd7/baseline-verification.json)
- [完整性核对](../../runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/baseline_20261113-152614_1dcd2fd7/artifact-integrity.json)
- [官方逐题报告](../../runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/baseline_20261113-152614_1dcd2fd7/logs/run_evaluation/baseline_20261113-152614_1dcd2fd7/coding-agent-baseline/sympy__sympy-15308/report.json)
- [原始测试输出](../../runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/baseline_20261113-152614_1dcd2fd7/logs/run_evaluation/baseline_20261113-152614_1dcd2fd7/coding-agent-baseline/sympy__sympy-15308/test_output.txt)

安装、镜像身份、构建、预检、命令、进程输出与数据来源核对均在同一目录保存。未来 Agent 轮已追加 baseline-ready.json，未创建模型 attempt.lock；原始准备、失败轮与报告不覆盖。本机被忽略材料不代表远端备份。

下一步先将镜像源码校验与 LF 写入处理接入正式评分流程并定向验收，再明确模型、token/时间预算与调用授权，运行本题 Agent、保存 P、另行官方评分。当前没有真实模型成绩，也没有全量 benchmark 成功率。
