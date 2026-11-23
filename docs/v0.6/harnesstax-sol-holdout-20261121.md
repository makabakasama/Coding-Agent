# GPT-5.6 Sol 首批正式留出预登记

2026-11-21。已登记三道 SWE-bench Lite `test` 题及三道有序替补。原首选均通过容器基线和独立评分预检，无替补。网关实际计价和付费启动授权待确认。见[基线验收](harnesstax-sol-holdout-preflight-20261121.md)。

从 HarnessTax 作者的逐题图表中只提取 `task` 字段，核对 16 组数据覆盖同一组 30 道题，再与固定版本的 Lite test 取交集。按本地运行记录、已有题单、调优文档和工作副本排除 5 道已接触题。剩余 25 道按 `SHA-256(seed + ":" + instance_id)` 升序排列，种子为 `harnesstax-sol-holdout-20261121-v1`。样本选择未使用逐题分数、补丁或隐藏测试内容。 [作者逐题数据](https://harnesstax.github.io/data/charts/system-harness-effect-swe.fe756518da.json)

| 顺序 | 首批 | 替补顺序 | 替补 |
| --- | --- | --- | --- |
| 1 | `django__django-14608` | 1 | `psf__requests-1963` |
| 2 | `scikit-learn__scikit-learn-14983` | 2 | `django__django-12497` |
| 3 | `sympy__sympy-23117` | 3 | `sympy__sympy-14024` |

六道题的固定源码、题面哈希、镜像摘要和完整剩余顺序存于 `runs/holdout_20261121-110927_86e147f5/` 的 `registration.json` 与 `public-preflight.json`。首发前检查已登记的三道基线。仅在付费启动前对确认的环境阻断或评分不可验证使用预登记替补，保留原题、故障和替补位置。付费启动后的未通过、空补丁和提前结束进入原题台账。若评分异常，暂停后续运行并优先用同一补丁补评。

使用与 Sol 开发校准字节相同的 91 个 Harness 源文件及其归档清单。使用 `gpt-5.6-sol`，每题一次、500K 本地计账额度、50 次决策、30 分钟软时限、传输重试 0。三题名义本地额度共 150 万 Tokens。串行执行，评测容器限额 1 GiB。冻结评分器为 `scoring-gate.1` 与 SWE-bench 5.0.2，独立复核补丁、有效镜像、报告和日志一致性。真实网关金额待确认。

外部 Pi + GPT-5.6 Sol 的公开单次解决率为 74.44%，平均每次 $0.440661，口径为 30 题、每题 3 次。首批三题虽来自同一公开题集，样本、重复数和计价条件仍有差异。按各自口径列示，待更充分的同条件证据再判断 Harness 收益。 [作者汇总数据](https://harnesstax.github.io/data/charts/system-frontier-swe.66946bd77f.json)
