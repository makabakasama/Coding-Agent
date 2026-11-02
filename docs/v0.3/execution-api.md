# M3 普通任务执行契约

日期：2026-10-31。阶段：**已实现并完成前端合成离线接入**。实际验证与限制见 [M3 验收](m3-verification.md)。M1 与 M2a 记录保持不变；本契约只贯通修复任务，不替代既有 G/R/P 实验。

## 启动和权威状态

`GET /api/tasks/{id}/execution` 始终只查询。未启动返回：

```json
{"schemaVersion":1,"taskId":"11111111111111111111111111111111","status":"not_started","canStart":false,"startBlockReason":"PREPARATION_REQUIRED","source":"configured_provider","isExecutionSandbox":false}
```

`canStart` 是后端重新核验准备记录、固定 commit、登记身份、干净副本、本机执行配置、全局互斥后的结论，不由前端从 prepared 推导。可启动时 startBlockReason 为 null。其他阻止原因：WORKSPACE_UNAVAILABLE、WORKSPACE_DIRTY、EXECUTION_CONFIG_UNAVAILABLE、EXECUTION_BUSY、EXECUTION_UNRESOLVED、ALREADY_STARTED。配置错误细节不公开路径/环境。

用户单独点击后 `POST /api/tasks/{id}/execution`，JSON `{}`；禁止携带模型、密钥、命令或测试目标。首次成功 **202**，立即返回 running 记录，后台执行，不占住 HTTP 连接。同任务重复 POST **200** 返回现有记录，永不第二次调用模型。准备/身份/配置/全局占用不满足返回 **409**，错误 code 为上述 startBlockReason，沿用 M1 error envelope。无效请求体 400 VALIDATION_ERROR。响应丢失后 GET 查询，刷新绝不 POST。

开始后返回以下公共字段；时间均 UTC RFC3339：

```json
{
  "schemaVersion":1,"taskId":"11111111111111111111111111111111",
  "executionId":"22222222222222222222222222222222",
  "status":"running","canStart":false,"startBlockReason":"ALREADY_STARTED",
  "source":"configured_provider","isExecutionSandbox":false,
  "phase":"agent","activity":"model_or_tool","steps":0,"maxSteps":12,
  "startedAt":"2026-10-31T08:00:00Z","updatedAt":"2026-10-31T08:00:00Z",
  "finishedAt":null,"liveness":"confirmed","stopReason":null,
  "resultAvailable":false
}
```

status：not_started、running、finalizing、terminated、unknown。phase：agent、final_checks、delivery（未启动/unknown 可无 phase）。活动文字是后端枚举，不是模型自由说明；steps 是已记录的模型决策步号，不把工具数算作步数。updatedAt 是保存记录时间，不是心跳；浏览器不得用停更推断完成或死亡。

source 为 configured_provider（生产默认，已有 Provider/.env）或 injected_test_model（仅程序注入测试模型，必须显式标识，无生产离线演示 CLI）。模型只在主动 POST 后调用。独立演示路由不触发真实任务。

同一数据根目录只允许一个执行者，OS 全局文件锁覆盖线程及服务进程。执行目录是永久一次性声明。重启/崩溃后未终结记录返回 unknown、liveness=unconfirmed，不能自动重新调用模型。遗留未解决全局 owner 也阻止其他任务启动（EXECUTION_UNRESOLVED）；本版无恢复/取消/解锁接口，需维护者先核实旧执行及子进程，不自动清理材料。终态 liveness=stopped。终态缺失结果文件时 resultAvailable=false，不伪造结果。

## 增量事件

`GET /api/tasks/{id}/execution/events?after=0&limit=50`。after 为最后消费的非负整数序号，limit 1–100；非法/重复/未知参数返回 400 INVALID_CURSOR。返回：

```json
{"taskId":"11111111111111111111111111111111","events":[{"seq":1,"at":"2026-10-31T08:00:00Z","type":"phase","phase":"agent"}],"nextCursor":1,"hasMore":false}
```

事件序号从 1 递增，持久化后可跨刷新分页，不重新编号。只提供受控摘要：phase、decision（step/action）、observation（step/action/ok/feedback）、model_attempt（step/attempt/outcome）、finish、limit、error、terminated；不向浏览器转发模型原始响应、工具参数、代码正文、堆栈或私有 trace 路径。无事件时数组为空，nextCursor 保留 after。客户端可运行中定时 GET；导航后旧响应不得覆盖新任务。无 SSE/WebSocket。

observation.feedback 是运行中“最近一次实际反馈”的权威摘要：`{kind,status,message,messageTruncated,redacted:true}`，message 最多 400 字符。读取/搜索/列出反馈可增加仓库相对 `path`、`returned`、`totalLines`；编辑反馈可增加 `paths`、`applied`；测试反馈可增加 `passed`、`exitCode`、`timedOut`、`counts`（定义如下）。失败 message 显示脱敏原因；不返回源码正文或原始参数。测试事件仅在该次测试完成后发布摘要，无逐字流式 stdout。运行中可见全部这些摘要；完整工具反馈/Agent trace 仅本机保存，不公开下载；最终固定检查的 stdout/stderr 才有受控材料引用。未提供字段表示没有这类证据，不能补成 0 或成功。start 事件仅标记原循环已开始，不含原任务正文。

## 结果和受控材料

`GET /api/tasks/{id}/execution/result`：200 返回下列结果；尚无完整结果返回 **409 RESULT_NOT_AVAILABLE**。unknown 可保留已产生材料，但不推断完成。

```json
{
 "schemaVersion":1,"taskId":"11111111111111111111111111111111","executionId":"22222222222222222222222222222222",
 "source":"configured_provider","stopReason":"agent_reported_finished",
 "agent":{"status":"agent_reported_finished","steps":3,"summary":"模型说明，仅用于解释"},
 "finalChecks":{"status":"passed","targets":["tests/test_calc.py"],"checks":[{"target":"tests/test_calc.py","status":"passed","exitCode":0,"timedOut":false,"counts":{"total":1,"passed":1,"failed":0,"errors":0,"skipped":0},"stdoutArtifactId":"check-1-stdout","stderrArtifactId":"check-1-stderr"}],"reason":null,"counts":{"total":1,"passed":1,"failed":0,"errors":0,"skipped":0}},
 "review":{"status":"passed","changedPaths":["calc.py"],"outOfScopePaths":[],"protectedChanges":[]},
 "artifacts":[{"artifactId":"diff","kind":"diff"},{"artifactId":"events","kind":"events"},{"artifactId":"check-1-stdout","kind":"test_stdout"},{"artifactId":"check-1-stderr","kind":"test_stderr"}],
 "patchExport":{"status":"saved","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","bytes":157,"reason":null},
 "incomplete":[],"independentlyVerified":false
}
```

stopReason：agent_reported_finished、step_limit、agent_error、execution_error、policy_violation。它只表示停止原因。agent.status 保留原循环结论；finalChecks.status 为 passed、failed、skipped、not_run，不能由 finish 或循环中最近一次测试推导。固定检查按 checkTargets 原顺序逐项执行；allowTests=false 为 skipped / TESTS_NOT_AUTHORIZED。无法安全继续检查时 not_run，附原因。每项 check.status 为 passed、failed、skipped、not_run；exitCode 缺失为 null，超时单列，输出为空与未执行分别处理。任一失败整体 failed；所有实际检查均跳过则整体 skipped，绝不 passed。每次最终检查后再次核验身份和范围；出现测试副作用则 review failed，不因退出码 0 宣称交付通过。

每个已运行 check 增加 `counts: {total,passed,failed,errors,skipped}`，各值非负整数；来自该次 pytest 的独立 JUnit XML 记录（仅本机，保存时过滤已知密钥），无法读取/校验时 counts=null、reason=TEST_COUNTS_UNAVAILABLE（超时/进程或日志错误优先记录其原因），不能判 passed。exitCode=0 且 passed>0、failed=errors=0 才可 passed；全跳过为 skipped / ALL_TESTS_SKIPPED；total=0 为 failed / NO_TESTS_COLLECTED；非零退出、超时、收集/环境错误均 failed。混合通过/跳过保留真实 skipped 数量；pytest xfail 在 JUnit 中按 skipped 记，不伪造独立 xfail 数。finalChecks.counts 汇总实际计数，任一已运行项计数未知则 null；未运行/未授权时 null。不得把 nullable 计数显示成零。

review.status 为 passed、failed、unavailable。越界、测试/配置变动或身份漂移会写入结果，保留 trace/diff/输出；incomplete 明确未完成项。最终检查只覆盖配置目标，始终 independentlyVerified=false，不使用 independently_verified 标签。

`GET /api/tasks/{id}/execution/artifacts/{artifactId}?offset=0&limit=16000`：仅允许结果登记的材料 ID，拒绝目录/路径输入。200 返回 `{artifactId,kind,content,offset,nextOffset,totalChars,hasMore,redacted:true,truncated:false}`；offset 为字符偏移，limit 1–16000；非法参数 400 INVALID_CURSOR，未知 ID 404 ARTIFACT_NOT_FOUND。完整结果尚无时 409 RESULT_NOT_AVAILABLE。材料不可读 503 STORAGE_UNAVAILABLE。单份网页材料最多公开前 2 MiB 输入字节，超过则 truncated=true；本机脱敏日志不受网页此长度截断。分页不代表原始进程输出完整，前端须展示 redacted/truncated/缺失含义。

只公开脱敏文本 diff、事件摘要及实际测试 stdout/stderr。trace、路径、解释器、非敏感环境与模型连接的非敏感元数据只留本机；没有任意文件下载。已知密钥与本机绝对路径在公共文本中替换，用户仍不应在需求或仓库正文填写密钥。测试 stdout/stderr 和 JUnit 本机记录也过滤已知密钥，是脱敏记录，不承诺与进程原始字节一致。

脱敏网页 diff 不能作为字节一致的补丁验收材料。结果增加 `patchExport: {status,sha256,bytes,reason}`：无已知密钥且可完整提取时，本机 candidate.patch 保存提取补丁的准确字节及 SHA256，status=saved；若检测到已知密钥，则不写 candidate.patch，status=not_exported、reason=KNOWN_SECRET_DETECTED、sha256/bytes=null，并在 incomplete 说明保留副本待审查。无法安全提取部分变动时 not_exported / INCOMPLETE_DIFF，网页 diff 也可能不覆盖全部变动，incomplete 会说明。提取/保存失败为 unavailable / DIFF_UNAVAILABLE。没有原始补丁下载 API，不为保存原件而把已知密钥写入日志。补丁提取包括跟踪文件 diff 和可支持的新建业务文本文件；审查或提取无法覆盖的材料会列入 incomplete，不能宣称可重放或独立验收。

## 执行边界和本机配置

启动前再次验证 detached HEAD 等于 baseCommit、源 common-dir/独立 Git 目录及预期副本位置一致；副本必须干净（包含未跟踪/忽略文件）。工具实际编辑只允许 allowedPaths 覆盖的文件/目录；拒绝链接/路径逃逸、受保护目录/密钥、测试与检查配置编辑。M3 对 raw patch 的重命名、复制、二进制/文件模式变更不支持，保守拒绝；不弱化已有实验工具。检查目标只能取冻结 checkTargets，模型不得改成 '.' 或其他目标。测试本身执行仓库代码，worktree 不是沙箱。

本机 projects.json 向后兼容三字段项目，新增可选 `execution: {testPython, testEnv, testTimeout}`。testPython 相对配置目录解析，缺省为运行服务的 Python；testTimeout 整数 1–300，缺省 90。testEnv 仅允许 PYTHONHASHSEED、PYTHONUTF8、PYTHONIOENCODING、PYTEST_DISABLE_PLUGIN_AUTOLOAD 的非敏感字符串；测试额外固定禁用自动插件及外部缓存，拒绝 PYTHONPATH、密钥或任意环境注入。配置在启动时快照，运行期间不随配置变化。浏览器不接收配置内容。既有 .env 仅用于现有 Provider，不复制到副本或公共材料。

实现不提供执行沙箱；本机可信项目的测试可能产生副作用或访问网络。最终审查能发现工作副本变动，但不是对任意代码的隔离保证。此批不改造 #5998/#10525 的准备/修复/独立验收入口。
