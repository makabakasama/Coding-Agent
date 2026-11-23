# P0 两题模型复测

2026-11-17 按复测计划使用 gpt-6-luna 对 Django 11019 和 SymPy 14817 各运行一次，沿用固定 G、已验收官方基线和每题 500000 token 计账上限、1800 秒软超时。两题模型均报告 incomplete，官方均未通过。容器串行执行，最终无运行中容器，WSL 内存上限保持 8 GB。未重新执行官方 G/R 基线，沿用各自已验收报告。

## Django 11019

轮次 `run_20261117-162551_63a4429a`，15 次模型调用，Provider 报告 137771 token。模型写入修改，运行临时脚本，基线和候选断言均失败，随后报告 incomplete。模型阅读了 `tests/forms_tests/tests/test_media.py`，但没有调用该既有测试。因此本次模型轨迹没有触发 Django 原生测试入口，不能以此宣称模型实际使用新入口。该入口此前无模型真实容器验收 17 项通过，见 [P0 验收](p0-acceptance-20261117.md)。官方评分 fail，resolved=false。

## SymPy 14817

轮次 `run_20261117-163032_11de5b89`，15 次模型调用，Provider 报告 126053 token。模型先尝试 `sympy/printing/tests/test_pretty.py`，得到 File missing，随后读到正确路径 `sympy/printing/pretty/tests/test_pretty.py`，该文件约 137 KB；它修改代码、运行临时检查及既有测试，并报告 incomplete。候选既有检查报告 111 passed、2 failed，原始输出还有 3 个异常；基线 113 passed、3 errors，比较结果 incomplete。官方评分 fail，resolved=false。

此前将该任务三次 `File missing or too large` 归因为文件超过 800 KB 不成立：三次请求均指向不存在的 `sympy/printing/tests/test_pretty.py`，旧工具把缺失和过大合并报告。本次正确路径可读取，但小于 800 KB，不能把它作为真实模型使用大文件读取能力的证据。大文件行为仅有此前离线工程验收。

临时检查候选通过、基线报 execution_error，系统比较标记 incomplete，不能仅凭模型答复断言基线测试已有效复现目标。官方评分和开发检查分别留在各轮；此处不把模型自报成功作为验收。

两题各一次，只是受影响开发案例，无法证明 P0 提升了成功率。此前 Django 原生入口已做非模型容器检查；模型是否会主动调用、真实大文件读取编辑以及不同任务泛化仍需后续样本或指定检查。模型补丁、轨迹及评分保存在被忽略的 `runs/evaluation/swe-bench/` 下，本地留存不代表远端备份。
