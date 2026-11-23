# 新模型 gpt-6.1-sol 两题复测

2026-11-17。用户明确授权真实付费调用模型、新建两个 run，用已换好的 `gpt-6.1-sol` 对 django__django-11019 与 sympy__sympy-14817 各跑一次。沿用固定 G、原题输入、已验收官方基线、标准提示、50 决策 / 500000 token / 1800 秒 / 128000 上下文 / 8192 输出，串行单容器。上轮 gpt-6-luna 的两个 run 完全未改动。

## 结果总览

| 题 | run | 模型 | 调用 / token / 秒 | 官方 resolved | FAIL_TO_PASS | PASS_TO_PASS |
| --- | --- | --- | --- | --- | --- | --- |
| Django 11019 | `run_20261117-173257_10044c44` | gpt-6.1-sol | 23 / 385596 / 440.8 | **false** | 2 通过 / 14 失败 | 58 通过 / 0 失败 |
| SymPy 14817 | `run_20261117-173337_65acdba8` | gpt-6.1-sol | 25 / 481906 / 367.5 | **false** | 0 通过 / 1 失败（`test_MatrixSymbol_printing`） | 111 通过 / 0 失败 |

对照上轮 gpt-6-luna（均 resolved=false）：Django 15 调用 / 137771 token / 160.1 秒；SymPy 15 调用 / 126053 token / 153.1 秒。新模型调用数和 token 约为上轮 2.8–3.8 倍。

`execution-config.json` 确认实际生效模型为 `gpt-6.1-sol`（`source=configured_provider`、`profile=gpt`）。两轮官方评分 `patch_applied=true`、`infra_failure=false`、`patchComparison=exact_bytes`，补丁 SHA 与评分组一致；容器最终无运行中实例。

## Django 11019：明显进步但仍未通过

上轮失败的直接原因是补丁漏改 `merge` 的可变参数签名、对 tuple 输入报 TypeError、输出类型不符。新补丁针对性改对了：
- `merge(list_1, list_2)` 改为 `merge(*lists)`，`_css`/`_js` 改为 `self.merge(*lists)` 传入任意数量列表，并保留单列表时的 tuple 表示。
- 用 `heapq` 做稳定的依赖合并，真环才告警。
- 模型报告运行了既有 `tests/forms_tests/tests/test_media.py` 且 17 项通过，并在第一次检查发现 tuple 兼容问题后自行修正。

官方结果从 0/16 提升到 2/16 FAIL_TO_PASS 通过，PASS_TO_PASS 58 项零回归——工具障碍与部分行为障碍已消除。但仍有 14 项目标测试失败（如 `test_combine_media`、`test_construction`、`test_form_media`），合并输出仍未满足全部目标断言。模型自写回归脚本因缺 Django settings 未能执行，被报告为 incomplete。

## SymPy 14817：目标测试仍未通过

上轮失败的直接原因是把 `self._print(item)` 改成 `self._print(-item)` 造成双重取负、丢掉负号。新补丁避开了这个坑：
- 判断条件由 `S(item.args[0]).is_negative` 改为 `pform.binding == prettyForm.NEG`，用渲染结果而非 sympify 名字字符串判断负项。
- **删除**了 `pform = self._print(-item)` 这一行，不再重复渲染负项。

官方 `test_MatrixSymbol_printing` 断言 `pretty(A*B*C - A*B - B*C) == "-A*B -B*C + A*B*C"` 仍失败；PASS_TO_PASS 111 项零回归（上轮候选有 2 项失败，其中 `test_Adjoint` 回归已消除）。模型报告 focused 回归脚本通过、既有套件因基线既有错误无法完整验证，结论 incomplete。

## 结论与边界

- 换用 `gpt-6.1-sol` 后，两题官方**仍未通过**（resolved=false），但都不是工具障碍或基础设施失败：Django 的目标通过数从 0 提升到 2、零控制回归；SymPy 消除了上轮的 `test_Adjoint` 回归，剩 1 项目标断言未满足。
- 两题模型都主动运行了既有测试并据此自我修正，说明上一轮「模型不跑既有测试」的问题在新模型上未复现；但这些自我修正仍不足以通过官方目标测试。
- token 成本显著上升（合计约 86.8 万 vs 上轮约 26.4 万），未转化为官方通过。
- 两题各一次、均为已知开发案例；单次结果不能证明模型或 P0 改动的稳定效果。官方评分与开发检查分别留存，模型自报不作为验收。运行材料在本机被忽略的 `runs/` 下，不代表远端备份。
