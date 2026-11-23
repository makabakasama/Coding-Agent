# Coding-Agent 最终成效评测计划

2026-11-20。首轮按用户最终指定，使用 Coding-Agent + `gpt-5.6-sol` 对 SWE-bench Lite dev 的 `pvlib__pvlib-python-1606` 运行一次，预算 500K 本地计账 Token。官方评分通过，实报 469,055 Tokens。开发校准单列。见[实测结果](harnesstax-sol-dev-result-20261120.md)。

首轮复用无付费预检登记的兼容镜像、公开测试和官方评分基线。见[开发题预检记录](harnesstax-luna-dev-preflight-20261120.md)。

2026-11-21 从 HarnessTax 公开题号预登记的首选三题均通过预检，经用户授权以 GPT-5.6 Sol 串行各跑一次，独立官方评分 3/3。网关实报 1,135,600 Token，另有两次请求用量未知。三道有序替补均未启用。见[首批正式留出预登记](harnesstax-sol-holdout-20261121.md)、[基线验收](harnesstax-sol-holdout-preflight-20261121.md)及[正式结果](harnesstax-sol-holdout-result-20261121.md)。

2026-11-21 按原候选序列独立登记第二批三题并完成无模型预检。Django 12497、SymPy 14024 为 READY，Requests 1963 因两项官方控制测试遇到 TLS 校验错误，基线 UNSCORED。保留三题，未启动替补或模型。见[第二批预登记](harnesstax-sol-holdout-batch-02-20261121.md)及[预检结果](harnesstax-sol-holdout-batch-02-preflight-20261121.md)。

2026-11-21 两道 READY 任务按授权各运行一次。Django 12497 官方通过，SymPy 14024 无提交，Requests 1963 保留未准入。两批合计 5 次正式运行、4 道官方通过。见[第二批正式结果及累计用量](harnesstax-sol-holdout-batch-02-result-20261121.md)。

2026-11-23 五条旧轨迹完成测试回执离线回放。候选回执估算少传 102,543 输入 Token，主要来自 SymPy 14024。该题第 20 步在历史结算下仍无法放行普通请求，计入此前估算节省后可放行一次重建请求。本轮只验证归档与预算反事实。见[回放报告](check-receipt-replay-20261123.md)。

## 外部参照

HarnessTax 作者在 30 道 SWE-bench Lite 题上每题重复 3 次，Pi + GPT-5.6 Luna 的平均单次解决率为 53.33%（95% 区间 35.56%–70.00%），每次平均费用按其价表折算为 0.029976 美元。本项目直接引用公开值，无需本地部署、预检或运行 Pi。若样本、重复、预算或价表不同，分别展示并注明差异。[研究方法](https://harnesstax.github.io/) · [作者图表数据](https://harnesstax.github.io/data/charts/system-frontier-swe.66946bd77f.json)

## 模型和计费预检

| 候选模型 | OpenAI 公布的 API ID | 当前项目网关 |
| --- | --- | --- |
| GPT-5.6 Sol | `gpt-5.6-sol` | 首轮 21 次响应均返回该 ID，网关费率未核验 |
| GPT-5.6 Luna | `gpt-5.6-luna` | 尚未核验该 ID 或等价映射 |

2026-11-20 只读核对发现，项目 `.env` 的 GPT 默认配置为 `gpt-6-luna`。首轮运行单独指定 `gpt-5.6-sol`。两款模型的网关计价待核对，`gpt-5.6-luna` 的网关可用性仍待核验。任何获准的真实探测均记录实际请求与返回的模型 ID、端点指纹、接口类型、推理档位和 Service Tier。 [Sol API 说明](https://developers.openai.com/api/docs/models/gpt-5.6-sol) · [Luna API 说明](https://developers.openai.com/api/docs/models/gpt-5.6-luna)

本地费用注明 Provider 价表、计价日期、币种及计价档位。逐次保留原始 Usage 与可归属的账单记录，统一输入、命中缓存的输入、写入缓存及输出的字段含义。Provider 已把推理计入输出时只计一次。按网关实际费率预算并归档费率。OpenAI 公布的 API 价格可作统一折算参考。 [OpenAI API 价表](https://developers.openai.com/api/docs/pricing)

## 开发估算和正式留出

1. **预先固定样本范围。** 从同一固定数据版本登记留出候选的选择规则、排除历史调优任务的清单、题目顺序和替补条件。正式规模随获批预算从登记顺序截取。开发、接入和费用探测只使用预先指定的独立开发题。
2. 使用 Sol 开发校准的真实用量与评分结果核对执行链路。取得可核验费率后按开发数据估算正式批次预算。
3. 正式留出题量和重复次数在启动前登记。评分环境异常沿用现有暂停和补评流程。模型运行仍按用户另行授权开展。

## 验收与简历口径

- 主指标按任务平均每次尝试获得官方通过的比例。每次尝试只提交一份补丁。各组列出登记数、已启动数、官方通过、官方失败、未评分及未启动。空补丁和超时留在已启动分母；评分故障先恢复同一补丁，仍缺失时注明有效分数与未决范围。若提前停止，注明覆盖范围。
- 总支出按每次请求归集，包含失败和重试。报告平均每次成本、每解决一题成本（全部尝试费用除以官方通过次数）、输入/输出及缓存用量。零通过时单位成功成本无定义。统一价表折算值与实际账单分列，未知用量单列；只有可核验费用才报告金额节省。
- 简历中采用自有正式留出集的实测成绩。Pi 成绩注明为 HarnessTax 外部参照；跨研究的数值差异不足以证明 Harness 的因果收益。
