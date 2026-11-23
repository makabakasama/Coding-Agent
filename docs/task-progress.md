# 执行上下文与步骤进度

V0.5 第三批已接入上下文、步骤、观察引用、结束校验及工作台展示。2026-11-10 用户明确授权后，指定跨文件主线及异常分支已通过定向离线验收。

## 输入组织

RunContext 每轮组装固定 system、冻结任务、累计 assistant/tool 历史及唯一最新 runtime_state。状态尾只进入当次请求，下一轮重新生成。Provider 保存工具定义快照，通过独立 tools 字段发送。

状态包含当前/剩余步骤、阻塞、编辑修订、检查状态、已用/剩余决策数及最近三个观察摘要。剩余次数包含本次决策，finish 和计划更新也消耗决策。trace 保存输入指纹、消息数和字符量，Provider 的可识别实际用量单独记录。

## update_plan

工具参数为 steps 和 reason。steps 为 1–8 项，每项包含 id、goal、status、evidence_refs、blocked_reason。

- id 为字母开头的稳定短标识，最多 32 字符；goal 最多 300 字符。
- status 为 pending、in_progress、completed 或 blocked，最多一项 in_progress。
- completed 至少引用一个本轮实际 observation_id；每项最多 8 个引用。
- blocked 必须给出原因，其余状态清空 blocked_reason；原因及修订 reason 最多 1000 字符。
- 允许追加、重排、重新打开步骤，已有 ID 和目标保持稳定。修订保存在 trace 和事件中。

计划在 Agent 内处理，文件权限继续由原有工具检查。观察引用的合法性与自然语言需求满足程度分别审查。

## 检查与退出

实际编辑递增 edit_revision，旧通过检查随之过期。已完成步骤引用旧通过检查时列入 stale_evidence_refs。建立计划后，正向 finish 会检查未完成步骤、过期检查及最后失败检查，必要时反馈继续执行或说明 incomplete/blocked。

正常结束、模型异常和预算耗尽均保留 progress 与剩余预算。界面展示最后保存的步骤、阻塞、修订和实际工具事实，固定最终检查独立展示。旧记录缺少 progress 时显示未提供，Worker 待核实状态继续由既有运行状态说明。

脚本模型可通过 core.context.latest_tool_result 定位最后工具结果，兼容末尾新增状态消息。已有示例与相关夹具已调整取值方式。

## 定向离线验收

tests/test_task_progress_acceptance.py 覆盖七个场景。主线通过保存任务入口新建模块、修改调用方、执行固定检查、更新引用证据的计划并交付，核对实际 Diff、状态、事件及结果接口。其余场景覆盖工具失败、过期证据、提前结束、预算耗尽、非法引用和模型异常。每轮实际模型输入均被捕获，检查反馈、剩余工作和预算在下一轮真实可见。

最终专项为 7 passed，连同相关 Provider/重试协议检查为 31 passed、57 subtests passed，前端 Node 检查为 66 passed。首次组合检查有两处旧测试按错误事件类型取字段的失败，修正为筛选重试事件后通过，原断言和首次输出保留。生产功能本轮保持原实现。

本轮仅定向检查，完整后端未重跑；模型为脚本注入，前端为 Node DOM 环境。输入、轨迹、冻结检查、Diff、输出和源码归档保存在本机 runs/engineering/v05-progress-acceptance-20261110/。

后续真实 Provider 已完成一次 QQ Bot 两文件编辑，17 次决策并保存计划与补丁；该轮禁用测试，未独立验收，不能把步骤完成当作业务验证通过。阶段结论见 [V0.5 收尾](v0.5-status.md)。
