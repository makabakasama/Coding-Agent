# 首批只读实验历史接口

日期：2026-11-02。已实现；实际4176读取证据见 [交付记录](history-verification.md)。基于 [数据模型](data-model.md)，只读现有两项目材料，所有新历史路由只允许 GET，不包含启动 POST。

## 项目与需求

`GET /api/projects` 继续返回 `{projects:[...]}`。普通已配置项目增加 `modes:["ordinary"], historyAvailability:"not_applicable", canCreateTask:true, readOnly:false`；登记的历史项目为：

```json
{"projectId":"browser-use","name":"browser-use","modes":["experiment"],"historyAvailability":"available","canCreateTask":false,"readOnly":true}
```

另一项目为 `pydantic-ai`。historyAvailability 为 available（历史根目录存在）或 missing；普通项目为 not_applicable。若本机也登记同ID普通项目，则合并 modes 并保留 canCreateTask=true，历史仍只读。普通任务表单只使用 canCreateTask 不为 false 的项目，不能因看见历史项目就提交创建。

任务详情另有 `legacyLinks`，统一为 `[{label,href}]`；本批两个实验任务都是空数组，因为未记录它们与某个32位普通任务的明确关联。普通任务列表/详情的现有入口保持不变，不猜造对应链接。

`GET /api/projects/{projectId}/tasks` → `{tasks:[{projectId,taskKey,mode:"experiment",issueId,title,requirementRef:null,requirementRevision:null,roundCount,historyAvailability,readOnly:true}]}`。两个 taskKey 分别为 browser-use-5998 / pydantic-ai-10525，issueId 为 "5998" / "10525"。`GET /api/projects/{projectId}/tasks/{taskKey}` 返回同一任务对象。当前只接这两个显式登记需求；普通任务仍使用既有 /api/tasks 及原32位链接。

## 轮次与验收

`GET /api/projects/{projectId}/tasks/{taskKey}/rounds` → `{rounds:[...]}`。每项与轮次详情对象字段相同，按实际导航 roundId 倒序；createdAt 只取原记录明确时间，不从目录ID推断。

`GET /api/projects/{projectId}/tasks/{taskKey}/rounds/{roundId}` →

```json
{
 "projectId":"pydantic-ai","taskKey":"pydantic-ai-10525","roundId":"20261029-152335_25271a9c",
 "mode":"experiment","entry":"run","sourceFormat":"experiment_round",
 "createdAt":"原报告时间或null","originalRunConclusion":"independently_verified",
 "preparationStatus":"baseline_verified","agentStatus":"agent_reported_finished","exportStatus":"candidate_exported",
 "verificationCount":1,"evidenceState":"available","association":"recorded",
 "requirementSnapshotRef":null,"baseCommit":"原记录准确commit或null","regressionSha256":null,"candidateSha256":null,
 "liveness":"not_assessed","integrityStatus":"not_checked","readOnly":true,"warnings":[],
 "roundReportRef":"round","originalRunReportRef":"experiment","preparationReportRef":"preparation","agentReportRef":"agent","candidateReportRef":"candidate",
 "artifacts":[{"artifactId":"preparation","kind":"report","label":"准备报告","available":true}],
 "verifications":[{"verificationId":"verify-01","sourceFormat":"verification_v1","createdAt":null,"rawStatus":"independently_verified","independentlyVerified":true,"reportRef":"verify-01-report","candidateSha256":null,"integrityStatus":"not_checked","tests":[],"warnings":[]}]
}
```

reportRef 均为 artifactId 字符串，缺失为 null；每个可用引用都在本轮 artifacts 数组中。固定材料包括 round、summary、experiment、preparation、agent、candidate、regression-patch、agent-patch；验收报告 ID 为 verify-01-report 等，测试输出以日志类ID登记。artifacts 每项固定 `{artifactId,kind,label,available}`，kind 为 report/summary/patch/test_stdout/test_stderr。不可用材料可留清单但 available=false。

`GET .../rounds/{roundId}/verifications` → `{verifications:[...]}`，与详情数组相同。verificationId 沿用 verify-01/02；现代报告 sourceFormat=verification_v1，旧 manifest 为 legacy_verification，旧格式 independentlyVerified=null，不能由旧状态升级为新验收成功。tests 仅提供原报告存在的 target/exitCode/timedOut/counts/matchesExpectation，缺值null。重复验收不算重跑模型，不覆盖 originalRunConclusion。

仅有归组 round.json、没有可关联标准记录的 legacy：association=unlinked、taskKey=null、evidenceState=unlinked；仍可通过其原登记根目录下的轮次导航查看，以“未关联材料”标记显示，首批可先使用 warnings 提示，不要求额外页面；不能声称已关联某轮修复。已知 roundId 保留，不新造ID。其余 evidenceState 为 available 或 partial；sourceFormat 为 experiment_round/legacy_round。历史running原值不等于当前存活。warnings 包含缺失/不可读/旧格式说明及原notes（脱敏）。

## 受控材料

`GET .../rounds/{roundId}/artifacts/{artifactId}?offset=0&limit=16000` 复用执行材料分页响应：`{artifactId,kind,content,offset,nextOffset,totalChars,hasMore,redacted:true,truncated}`。offset 为字符偏移，limit 1–16000；只读取本轮登记材料，最多2 MiB输入字节，超过则truncated=true。JSON报告用脱敏JSON文本，原字段保留但环境/解释器/源码绝对路径等本机配置值隐藏；文本中的绝对路径与已知密钥脱敏。页面只按文本展示，不将其作为原始可重放补丁。

未知项目/需求/轮次返回404 HISTORY_NOT_FOUND；未知或缺失材料404 ARTIFACT_NOT_FOUND，已知材料不可读503 STORAGE_UNAVAILABLE；分页错误400 INVALID_CURSOR；非GET为405。报告字段缺失返回null/partial，不补造；无法读取轮次元数据时不伪造轮次，列表warnings提示。仅构造已有Spec来确认登记身份，不执行准备/Runner/模型/测试，不重写任何历史材料，也不填补历史缺失G/R/环境。
