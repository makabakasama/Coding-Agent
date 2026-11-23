# DEBUG-01 修改未命中实际分派路径

2026-11-17 归档 SWE Lite `sympy__sympy-15345` 的真实失败。运行 `run_20261117-102834_53da8de8`，模型 gpt-6-luna，固定 G 为 `9ef28fba5b4d6d0168237c9c005a550e6dc27d81`。19 次模型调用，Provider 报告 204569 token。模型最终 outcome=incomplete，开发检查失败，官方目标 test_Function 失败、8 项控制通过，infra_failure=false。

## 失败链路

任务要求修正 Max 的 Mathematica 打印。Agent 在第 7 步增加 Max/Min 函数映射，第 10 步自测报 lambda 参数数量错误，traceback 明确指向父类 codeprinter.py 的 _print_Function。第 12 步将 lambda 改为接收任意参数，随后检查变为普通断言失败。第 14 步读取了父类函数，读取范围为 340–390 行，其中包含 `_print_Expr = _print_Function`。

第 16 步模型继续修改子类 MCodePrinter._print_Function，添加 Max/Min 分支。第 17 步断言仍失败，第 18 步仅回读检查脚本，第 19 步报告 incomplete。未运行既有 Mathematica 测试文件。

静态源码显示 Max 继承 MinMaxBase 与 Application，MinMaxBase 继承 Expr。Printer._print 按表达式类型的 MRO 顺序寻找 `_print_<类名>`。本例先匹配到父类 CodePrinter._print_Expr，该名称在类定义时绑定父类 _print_Function。子类重新定义 _print_Function 后，继承来的别名仍指向父类函数。因此子类新增 Max/Min 分支没有命中该分派路径。第一次检查的实际 traceback 与这条路径一致。后两次只有断言失败，本轮未追加运行时探针，路径结论依据固定源码和已保存的首次 traceback。

## 自测反馈缺口

脚本精确断言输出等于 Max[x,2]，没有在失败信息中带上实际输出。它同时绑定括号、参数顺序和空格，并增加任务未要求的 Min 检查。后两次只返回 AssertionError，缺少实际字符串，无法直接辨别修改是否影响输出。

模型曾读取父类别名所在行，却未进一步检查 Max 的继承链或确认选中的处理函数。现有轨迹可以确认遗漏了可用线索，无法推断模型内部原因。最后一次请求 mode=normal，最终回答前累计计账 184905 token，模型主动报告 incomplete；此次结束未触发 finish-only 或预算耗尽。

需求映射多次提供，但引用存在无效项。没有证据证明需求映射机制导致本次失败。输出格式的完整正确性仍需官方验收，不能把本次静态路径诊断直接当作已验证修复。

## 后续诊断流程

先查看失败输出的实际值及完整 traceback，再检查表达式类型、继承链和实际选中的打印方法。修改应针对被调用的路径，每次修改后观察实际输出变化。自测失败应带上 actual 与 expected 的 repr，并把核心缺陷和附加场景分开验证。随后执行相关既有回归和独立官方评分。以上流程尚未在本案例追加执行。

## 原始证据

28 份文件按字节复制并核对 SHA256，保存在被本案例 .gitignore 忽略的 raw/，源材料保持原位。raw/manifest.json 保存来源、大小和 SHA256，raw/selected-events.json 保存关键决策及原始 trace 行号。包括完整 trace、任务、补丁、结果、自测、三次开发检查、官方成绩和固定源码。原始材料为本机留存，未备份到远端。

模型补丁 SHA256 为 `0c3ddd566fc16b2005120d0931a0fe23cb63113a369f92bf282769f483f98e67`。该题已用于开发诊断。后续同题对照应标为开发对照，评测输入不得包含本案例、隐藏官方断言或事后诊断答案。
