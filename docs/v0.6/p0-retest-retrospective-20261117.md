# P0 两题复测轨迹复盘

2026-11-17 复测后的只读复盘。依据两轮保存的轨迹、开发检查记录和官方评分，核对「P0 改动是否真的被触发、失败原因是什么」。本轮不修改实现、不运行测试、不调用模型；结论区分轨迹事实与推断。

## 一、轨迹事实

### Django 11019（`run_20261117-162551_63a4429a`，15 次调用、137771 token）

- 模型检索并读取了 `django/forms/widgets.py` 的 `Media.merge`，随后读取 `tests/forms_tests/tests/test_media.py` 第 1–201 行（该文件共 573 行）。
- 首次 `replace_lines` 返回 `Replacement makes no change`，未改动文件；重新读取后第二次编辑成功，把合并逻辑改为先汇总序列再排序。
- 模型自写 `.agent-checks/check_media_merge.py` 并运行。检查在顺序断言处失败：实际得到 `['color-picker.js', 'text-editor.js', 'text-editor-extras.js']`，期望 `['text-editor.js', 'text-editor-extras.js', 'color-picker.js']`。
- 模型随即报告 incomplete，答复中明确写明「`test_media.py` was inspected but not run」。
- 全程没有任何一次 `run_tests` 指向既有测试文件，唯一执行的检查是模型自写临时脚本。
- 官方评分 `resolved=false`：FAIL_TO_PASS 16 项全部失败，均为 `forms_tests.tests.test_media` 及其他媒体相关既有测试。

### SymPy 14817（`run_20261117-163032_11de5b89`，15 次调用、126053 token）

- 模型先请求 `sympy/printing/tests/test_pretty.py`，得到 `File missing`；列目录后改读正确路径 `sympy/printing/pretty/tests/test_pretty.py`，该文件共 6147 行、约 137 KB。本次只读取第 1–80 行。
- 对 `sympy/printing/pretty/pretty.py` 的 `_print_MatAdd` 做了两次 `replace_lines`，改用 `could_extract_minus_sign()` 判断负项。
- 自写 `.agent-checks/matadd_pretty.py`，候选运行通过（exit 0）；基线运行为 `execution_error`，系统比较因此标记 `incomplete`。
- 随后运行既有 `sympy/printing/pretty/tests/test_pretty.py`：候选 111 passed、2 failed（`test_Adjoint`、`test_MatrixSymbol_printing`），基线 113 passed、3 errors。模型报告 incomplete。
- 官方评分 `resolved=false`：FAIL_TO_PASS `test_MatrixSymbol_printing` 仍失败。

> 轨迹中模型自述的候选/基线计数与结构化 `result.json` 的计数不完全一致；此处以 `result.json` 为准，不采纳自述计数。

## 二、P0 改动是否被触发

| P0 改动 | 原障碍 | 本轮是否被触发 | 判断 |
| --- | --- | --- | --- |
| P0-A Django 原生测试入口 | 既有测试走 `-m pytest` 报 `No module named pytest` | **否**。模型从未运行 `test_media.py`，只跑自写脚本 | 本轮无法检验 P0-A 的效果；无模型容器验收的 17 项通过仍是其工程证据 |
| P0-B 大文件分段读取 | 超过 800 KB 文件被 `read_file` 整体拒绝 | **否**。正确文件约 137 KB，低于阈值，且只读 1–80 行 | 本轮无法检验分段读取能力；此前三次 `File missing` 已确认是路径不存在，与大文件无关 |
| P0-B 有界编辑快照 | 编辑行号越界、过期快照覆盖 | **是**。两次 `replace_lines` 均基于快照成功应用，首次无变化被拒绝 | 常规文件上的快照编辑工作正常，但不是大文件场景 |

**核心结论：两题都用不上 P0 针对的障碍来判定效果。** Django 的模型选了自写检查而非既有测试，绕开了原生入口；SymPy 的目标文件不到阈值，绕开了大文件读取。两题官方未通过的直接原因是**补丁本身没有修对目标行为**（Django 合并顺序仍错；SymPy 目标 `test_MatrixSymbol_printing` 仍失败并引入 `test_Adjoint` 回归），不是工具障碍复发。因此这两次复测既不能证明 P0 提升了成功率，也不能证明 P0 无效。

## 三、开发检查分类核对

开发检查的分类逻辑工作正常，未把模型自报升级为验收：

- Django：`level=developer_check_failed`，`officialGrade=not_run`，`semanticCoverageVerified=false`；原因包含「既有仓库测试未在该版本运行」，与轨迹事实一致。
- SymPy 自写检查：因基线为 `execution_error`、比较为 `incomplete`，`level=verification_incomplete`，没有把「候选通过」当作验证成立。
- SymPy 既有测试：`level=developer_check_failed`，`repositoryRegression=failed_or_unknown`。
- 目标 testbed 解释器的语法检查在三个检查点均执行，`exitCode=0`，与两题补丁语法成立、失败在行为层面的事实相符。

一个值得保留的观察：两题里模型都**先写临时检查**，Django 甚至完全没有运行既有测试。原生入口与目标文件映射这类改动依赖模型实际调用，若只靠模型自主选择，可能根本不会被触发。这影响后续如何设计效果对照。

## 四、下一步范围（供决策，本轮未自行开工）

1. **要 P0 改动在「被触发时」的行为证据**：用固定补丁 + 固定任务、不调用模型，直接跑 Django 原生入口映射与大文件分段读写。成本最低、条件可控，能补上本轮缺的工程证据。
2. **要「模型是否会因 P0 改变行为」的证据**：需固定任务与指令，明确要求运行既有测试文件，再做受控对照。两题自然轨迹无法回答这个问题——模型本就没走那条路。
3. **不再追 P0 效果证据**：接受当前边界——工程验收充分、模型效果未证——转向下一工程重点或求职材料。

三条互斥，需用户指定后再推进。当前证据边界：P0 有离线与无模型容器验收，没有可归因于 P0 的模型效果证据；两题官方均未通过、模型均报 incomplete。
