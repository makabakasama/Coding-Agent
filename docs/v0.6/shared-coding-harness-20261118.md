# 普通编码任务与评测共用 Harness

普通编码任务、Chat 发起的编码任务和 SWE-bench 评测共用编码循环、累计预算与紧凑收尾。独立 CLI 同样使用共享预算，指定 `--check-target` 后也启用同一套开发检查证据流程。

- `repo_agent/core/coding_budget.py` 提供累计请求准入、Provider 用量结算、仅开放 `finish` 的收尾与结构化失败报告。普通编码默认累计 250,000 token、软超时 1,800 秒。可用 `CODING_AGENT_TOTAL_TOKENS`、`CODING_AGENT_SECONDS`、`CODING_AGENT_CONTEXT_TOKENS`、`CODING_AGENT_OUTPUT_TOKENS`、`CODING_AGENT_TOKENIZER` 配置。评测仍使用该轮显式配置的预算，Chat 编码仍沿用其已配置的单次上下文限制。
- `repo_agent/core/check_execution.py` 提供测试命令适配、失败阶段与结果分类。普通项目根据仓库实际入口选择 Django、SymPy 或 pytest，使用任务配置的解释器；对改过的 Python 文件在运行测试前做语法检查。容器评测把同一模块复制到随轮启动目录。
- `repo_agent/core/check_tools.py` 和 `repo_agent/core/check_quality.py` 提供临时检查路由、测试依据核对、同哈希复用与验证等级。普通任务和 Chat 编码继续限于冻结的测试目标，勾选临时 Python 自测后才允许在本机执行 Agent 编写的脚本。脚本位于运行材料目录，修复前后使用独立的脚本副本。能核验起始版本时在独立文件副本中复跑；无法重建时保留原因和未知结果。
- 使用授权的临时脚本自测可以帮助调试。没有指定固定最终检查时，交付中的固定检查标为未执行。已有独立评分、结果导入与官方容器管理仍由 Evaluation 执行。

普通开发检查使用本机的任务测试环境。文件副本隔离仅用于防止工作区和测试材料串扰。脚本与原生测试均以本机任务进程权限运行，Evaluation 继续用既有容器隔离。

本批新增通过普通任务入口验证临时自测前失败、后通过并完成交付的回归用例。当前只做了源码语法和调用链检查。普通任务、Chat 编码及容器评测的新接线尚待动态回归。
