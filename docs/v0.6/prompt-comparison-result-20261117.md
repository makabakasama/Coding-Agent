# Max 任务提示对照结果

两组各一次 gpt-6-luna，SWE Lite sympy__sympy-15345，固定 G、基线、模型、工具、权限、预算、容器及评分流程。单轮预算各 500000 token、软超时 1800 秒，串行运行。标准组 run_20261117-105104_a4a3dc72，精简组 run_20261117-105341_f7c824bb。实际仅调整 evaluation runner 的自测提示，系统提示仍要求多步骤任务调用 update_plan，工具 schema 和规则一致。提示 profile 和 SHA256 保存在各轮 prompt-profile.json，源码快照和实际请求保存在各轮。

标准组 13 次调用、Provider 报告 95429 token，精简组 11 次调用、报告 70720 token。两组模型均报告 incomplete。两组最终补丁都删除了 Mathematica 打印模块字典的闭合大括号，产生语法错误。开发检查在候选版本导入源码时失败，标记为基础设施检查异常，反馈未把语法错误清晰呈现为代码错误。

两个官方报告均记录 resolved=false、patch_successfully_applied=false，导入成绩 status=unknown。官方 run_instance.log 实际显示 git apply 成功，随后测试脚本运行，报告才显示该字段为 false。不能将该字段直接解释为 Git 补丁未应用，也不能将本次记为目标测试失败，两个报告都没有目标与控制测试结果。本轮需要进一步追踪官方输出和开发检查的错误反馈，才能判断这个报告字段的具体触发原因。

两个失败都发生在模型修改相同代码片段时，不能根据单题每组一次判断哪种提示更好。精简提示组本次 token 较少，但次数与上下文各不相同，不能归因于提示或推广为成本收益。两组最终容器均已清理，最终运行容器数 0。原运行、轨迹、检查与报告保存在 runs/evaluation/swe-bench 对应目录，本地材料不等于远程备份。
