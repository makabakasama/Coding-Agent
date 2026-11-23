# 两题复测「改了仍未通过」原因分析

2026-11-17。基于两轮已保存的 `candidate.patch`、模型轨迹、开发检查记录和官方 `test_output.txt` 的只读分析。未访问隐藏测试，未重跑容器，未改实现。下文的「事实」来自保存的输出，「推断」标注为推断。

## Django 11019：改了 `merge`，但没按目标测试要求改

题目：合并 3 个及以上 Media 对象时不再抛多余的 `MediaOrderConflictWarning`。模型把 `Media.merge(list_1, list_2)` 换成一个 Kahn 拓扑排序实现。官方 FAIL_TO_PASS 全部失败，根因有四类，前三类是模型补丁直接造成：

1. **没有改函数签名——目标测试要的是可变参数。**
   官方输出：`File ".../test_media.py", line 553, in test_merge` → `TypeError: merge() takes 2 positional arguments but 7 were given`，另有 `lists=()` 报 `missing 2 required positional arguments`、`lists=([1, 2],)` 报 `missing 1`。目标测试用 `Media.merge(*lists)` 传入 0、1、7 个列表，而模型补丁仍保留 `def merge(list_1, list_2)`。题目核心「3 or more」落在**参数个数**上，模型却理解成「两个列表的合并算法」。这是 FAIL_TO_PASS 全灭的主因。

2. **破坏了原有输入类型契约。**
   补丁首行 `combined_list = list(dict.fromkeys(list_1 + list_2))`。官方输出：`TypeError: can only concatenate list (not "tuple") to list`（`test_combine_media` 等）。CSS 值是 tuple，基线用 `list(list_1)` 接受任意可迭代对象；`list_1 + list_2` 只接受同类型序列。这使大量原本通过的媒体相关测试转为 ERROR。

3. **输出类型与目标期望不符。**
   官方 `test_construction`：`AssertionError: "Media(css={'all': ('path/to/css1', '/path/to/css2')}, js=('...',))" != "Media(css={'all': ['path/to/css1', '/path/to/css2']}, js=['...'])"`。目标期望 list，实际是 tuple。模型只改了 `merge`，没有触及构造/存储路径。

4. **合并顺序语义不符（推断，基于断言输出）。**
   `test_merge` 参数化用例：`AssertionError: Lists differ: [1, 2, 3, 4] != [1, 3, 2, 4]`。模型的拓扑排序按首次出现顺序做 tie-break，得到 [1,2,3,4]，与目标期望的交错顺序不一致。

**为什么模型自己没发现**：它读了 `test_media.py` 第 1–201 行却没运行该文件；唯一运行的是自写脚本 `check_media_merge.py`，而该脚本只覆盖题目描述的那一个例子、输入全是 list，且连它自己的断言都没通过（实际得到 `['color-picker.js', 'text-editor.js', 'text-editor-extras.js']`，期望 `['text-editor.js', ...]`）。模型随后仍报告 incomplete。目标测试关心的签名、输入类型、输出类型和顺序语义，自写检查一个都没覆盖。

## SymPy 14817：根因找到了，但改出了新的负号 bug

题目：`MatrixSymbol('y*')` 参与 pretty 打印时，`S(item.args[0]).is_negative` 对字符串 `'y*'` 做 sympify 抛 `SympifyError`。需求明确写着「不该用 sympify 处理来自 MatrixSymbol 的字符串参数」。模型把判断改成 `item.could_extract_minus_sign()`，**同时**把 `pform = self._print(item)` 改成 `pform = self._print(-item)`。

1. **双重取负导致目标测试仍失败。**
   目标测试 `test_MatrixSymbol_printing`：`assert pretty(A*B*C - A*B - B*C) == "-A*B -B*C + A*B*C"` 失败。基线代码对负项是「空格分隔 + 直接打印 `item`（自带负号）」，模型改成「空格分隔 + 打印 `-item`」。对已经是负项的 `-A*B`，`-item` 变成正的 `A*B`，负号被抵消。推断：这正是目标断言失败的直接机制——去掉 sympify 的方向对了，但把本就正确的负号打印路径一起改坏了。

2. **判断条件变化引入回归。**
   开发检查记录：基线 113 passed、3 errors；候选 111 passed、2 failed、3 errors。新增失败为 `test_Adjoint`（`assert pretty(Adjoint(X) + Adjoint(Y)) == " +    +\nX  + Y "`）和 `test_MatrixSymbol_printing`。推断：`could_extract_minus_sign()` 与原来的 `S(item.args[0]).is_negative` 对 Adjoint 的判断结果不同，使本该用 `' + '` 分隔的项改成了空格分隔，破坏了列对齐。

3. **3 个 exception 不是模型引入。**
   `test_issue_8292`、`test_issue_8344` 等的 `ValueError: Name node can't be used with 'False' constant` 在基线输出中同样存在（基线 3 errors），属环境/版本兼容的既有问题，与目标缺陷无关，不计入模型回归。

**为什么模型自己没发现**：自写脚本 `matadd_pretty.py` 只断言 `'y*' in result`、`'+' in result`、`'-' in neg_result`，是弱检查——输出里随便有个 `-` 就算过，完全没有断言 `-A*B -B*C + A*B*C` 这样的精确串。所以候选在自写检查中「通过」，给了模型虚假信心。模型确实运行了既有测试并看到 2 failed，但最终选择报告 incomplete，没有继续修复。

## 共同失败链

两题呈现同一模式，与 P0 工具改动无关：

| 环节 | Django 11019 | SymPy 14817 |
| --- | --- | --- |
| 目标测试当规格 | 读了但没运行既有测试 | 运行了但没有修到通过 |
| 自写检查强度 | 单一例子、list 输入、自身断言还没过 | 弱断言，不校验精确输出串 |
| 修改范围 | 只改 `merge` 算法，漏掉签名/类型/存储 | 改对判断条件，却改坏了负号打印路径 |
| 结果 | 目标 API 直接 TypeError | 目标断言与既有回归同时失败 |

结论：失败不是因为工具障碍（原生入口、大文件读取这两题都没触发），而是**修改没有覆盖目标测试定义的完整契约，且验证信号太弱**。P0 即便正确，也没有机会影响这两轮的评分。这一点限制了从这两题推导任何关于 P0 有效性的结论。
