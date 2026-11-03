# 项目、需求、轮次与独立验收的数据模型

日期：2026-11-02。**两个固定需求的新轮次启动及普通需求的新写入、多轮关系已实施；普通任务历史迁移及任意需求编辑尚未实施**。新普通任务采用统一目录，旧材料原位兼容。普通接口见 [ordinary-rounds-api.md](ordinary-rounds-api.md)，实验接口见 [history-api.md](history-api.md)。下文保留原设计过程，遇到首批只读或旧写入描述，以这两份当前增量契约为准。实施没有移动或改写历史材料。

## 1. 目标与现状

网页继续发展已有 browser-use #5998、pydantic-ai #10525 实验，统一为：

```text
项目 → 需求任务 → 多轮运行 → 每轮的零次或多次独立验收
```

当前已有两条实现路径，尚未统一：

| 实际代码/材料 | 已有能力 | 目标中的职责 |
| --- | --- | --- |
| `round_layout.py` | round.json、allocate_round、allocate_verification、角色副本命名和摘要 | 直接复用轮次与验收分配，不重新发明 round.json |
| `ExperimentSpec`、两个 examples 入口 | G、R、目标/控制测试、解释器、非敏感环境、可编辑文件及迁移映射 | experiment 模式的权威执行定义 |
| `ExperimentRunner.run_experiment` | 新轮次准备、模型运行、导出、独立验收和总报告 | 网页两个登记需求已复用此编排 |
| `ExperimentRunner.run_prepared` | 认领已准备 baseline，运行模型并导出 | 后续网页“从指定准备继续”复用；不是重复运行旧 baseline 的入口 |
| `runs/<实验名>/{run,prepare,legacy}_<ID>` | 两项目已有历史轮次及 verify-* | 首批只读历史数据源 |
| `task_store/task_service/task_execution`、`runs/tasks` | 网页普通任务保存、一次准备/执行、最近50条列表 | 已有普通任务兼容数据源，尚无任务下多轮模型 |

当前网页的 /api/tasks 并没有读取两个实验目录。普通任务已运行成功，也不意味着网页 experiment 模式或统一存储已经完成。既有 CLI 不需要为了本设计重写；普通任务执行器不能直接充当实验 Runner。

## 2. 一份本机项目注册配置

建议目标位置为 **`experiments/projects.json`**，利用已有忽略目录，存放本机项目身份与本机资源引用，与 runs 结果分离。不在每个任务、每轮目录各维护一份可编辑项目配置。

权威边界：

- 注册配置负责 projectId、name、本机 source/普通任务执行配置引用，以及允许的 experimentSpecId 与显式历史根目录绑定。
- experimentSpecId 如 `browser-use-5998`、`pydantic-ai-10525`，映射到后端登记的现有 `experiment_spec()` / Runner 构造入口。Spec 是 G/R、目标测试、解释器、环境及可编辑范围的权威源；注册表引用它，不再复制一份可漂移的同名配置。冲突须报配置错误，不能默默择一。
- 普通任务模式继续使用本机受限执行配置；实验模式使用其 Spec 和准备 manifest 的环境。pydantic-ai 的现有解释器及 `PYTHONPATH={workspace}/pydantic_ai_slim;{workspace}/pydantic_graph`（实际分隔符由 os.pathsep 决定）必须保留，不套普通任务 testEnv 白名单。解析已有记录时继续使用 recorded_environment/path_migrations，不全局改写字符串。
- 浏览器只提交已登记 projectId/taskKey/specId，不接受任意导入模块、仓库路径、解释器、环境字典或命令。模型密钥仍由已有本机 .env/环境提供，不放项目注册表或结果响应。

**当前仍是 `runs/tasks/projects.json`，本次不更换。** 首批只读阶段保留当前服务的配置来源，历史适配用后端明确登记的两个已知 Spec/历史根目录，不再创建第二份可编辑项目注册表。真正切换时单独实现配置加载与兼容：确认选定的一份权威文件，旧配置仅作显式兼容来源；若新旧同时存在且冲突则提示处理，禁止合并优先级猜测。路径切换/迁移另行授权。

## 3. Task 是需求身份，Round 是一次执行

### Task / 需求任务

最小字段：`projectId, taskKey, mode, issueId, title, requirementRef, requirementRevision`。

- taskKey 是项目内稳定需求身份及 runs 子目录键。已知实验保持 `browser-use-5998`、`pydantic-ai-10525`；issueId 分别为字符串 `5998`、`10525`。同一问题重跑模型仍属于同一 taskKey，产生新 roundId。
- requirementRef 指向当前可变的需求说明/登记入口，requirementRevision 标记当前内容版本；它是导航和下次启动的依据，不代表所有历史轮次都使用当前文字。
- 后续修改需求说明不修改历史快照。仅修订同一目标时保留 taskKey 并产生新需求版本；目标实质改变则另建任务，不能把不同问题强行归为一次修复。
- task 元数据拟存 `runs/<taskKey>/task.json`，是需求身份/当前引用，不是结果。旧实验目前未必有此文件，首批只读从显式 Spec/历史绑定产生导航视图，不补写历史文件。
- 现有网页 task.json 是固定的一份普通任务约定，不能直接当作“可变 task 元数据”原地编辑；只读兼容时按其保存内容展示，并保留原32位 ID。

### Round / 轮次

最小字段：`projectId, taskKey, roundId, mode, entry, sourceFormat, createdAt, requirementSnapshotRef, evidenceState`，加分离的准备/Agent/导出结论与报告引用。

- 复用现有北京时间 `YYYYMMDD-HHMMSS_<8位ID>`，由 allocate_round 分配；保留 round.json 现有 entry/name/round_id 字段。不能把 historical JSON 中旧 run_id 全部替换为导航 ID。
- 后续新启动的每轮在准备/调用模型前保存不可变的需求配置快照，建议文件名 `requirement-snapshot.json`。快照记录实际需求正文与版本、模式、Spec 身份及定义摘要、准确 G、R 引用/SHA256、目标与控制检查、可编辑范围、步数、解释器与非敏感环境模板等。候选 P 是随后产物，不能编入事前配置来充当已知结果。
- 引用可变分支时，启动准备阶段须解析并固定一次 commit；准备及后续阶段沿用该 commit。快照与准备 manifest 的实际 G/R 必须一致。运行参数不能跟随之后编辑的项目配置或需求说明漂移。
- experiment 模式的 `{workspace}` / `{run_dir}` 环境模板及每阶段实际解析值沿用 Spec/manifest 记录。快照是该轮证据，不升级为下一轮的可编辑项目注册配置。
- 现有历史轮次缺少此新快照时 `requirementSnapshotRef=null`，保留已有 manifest、agent-run.json 的实际 task/spec 相关字段及来源。不能用当前 TASK 常量或当前 Spec 补造历史快照，也不补写历史 round.json。

### 两种模式与结论

| mode | 执行与结果含义 |
| --- | --- |
| ordinary | 普通需求修复：准确起点、约定范围、Agent 结果、实际最终检查和修改审查；没有 G+R 缺陷复现证据时不能冒充 known-answer 实验。当前普通任务 independentlyVerified=false |
| experiment | 复用既有 Spec/Runner 的 G+R 准备、模型修复、P 导出和新副本独立验收；Agent finish、candidate_exported、验收结论分别展示 |

同一网页信息结构可以承载两种模式，但不能用普通任务的测试权限、环境白名单或结果标签改写实验语义。将来为 ordinary 增加独立验收也需明确其冻结检查和补丁重放流程，首批不包含此能力。

## 4. 只读适配历史证据

适配器先从已登记历史根目录读取 round.json，再读取实际存在的 experiment.json、repair/manifest.json、repair/agent-run.json、repair/artifacts/report.json 及 verify-* 下报告；summary.md/INDEX.md 仅辅助导航，不能取代原报告。

| 历史形态 | 只读呈现规则 |
| --- | --- |
| run_* | 一轮正式编排；保留原总报告结论及各阶段事实 |
| prepare_* | 准备轮次；后来经 manifest 运行模型仍在同轮，不能因目录叫 prepare 就断言从未调用模型 |
| legacy_* | round.json 提供导航身份，内部旧字段原样保留；以 sourceFormat 标出来源，缺字段为 null |
| verify-01/02/... | 每次独立验收是同一轮下的新条目，不等于模型重跑；原 experiment.json 的历史结论不被较新验收覆盖 |
| verify-*/manifest.json 旧格式 | 标记旧格式并保留 rawStatus，不能自动升级为新协议 independently_verified |
| legacy-unlinked / 缺失 round 元数据 | 只在未关联材料区展示；taskKey/roundId 不能确认则为 null，不按时间、近似文件名或邻近目录猜归属 |

任务/轮次归属使用显式注册绑定、round.json 和报告内可核对的链接。历史绝对路径在读取时使用已有 path-migrations.json 解析；原 JSON/trace/补丁/报告字节不改。不存在的副本只记缺失，不创建或回滚工作树。

报告缺少时间、模型、快照、哈希、退出码、测试计数或明确关联时返回 null，并给 evidenceState/warnings；没有验收报告是空验收列表，不是“验收失败”或“通过”。历史记录的 running 是当时原始状态，不能当当前进程存活；首批 `liveness=not_assessed`。

结果视图分列 `originalRunConclusion`、`agentStatus`、`exportStatus`、`verifications[]`，不合成为会覆盖过去结论的一个成功布尔值。现代验收只有原报告 status 与 independently_verified 一致支持时才标“报告记录独立验收通过”；这不表示本次重新验收。哈希字段来自原报告，未重新核验时 integrityStatus=not_checked，不把读到哈希当作已检查内容完整。

## 5. 已实现的历史与固定实验运行 API

现有 /api/projects、/api/tasks 和下列项目内任务/轮次路由已实现；字段用 camelCase，ID 由后端登记或证据映射，浏览器不传本机路径。两个登记需求另有 POST `/runs` 和 GET `/runs/status`，复用原 ExperimentRunner；历史 GET 仍不触发 Runner。

| 路由 | 最小响应字段 |
| --- | --- |
| GET /api/projects | 保留现有 projects 包装及 projectId/name；后续可增加 modes/historyAvailability，不能破坏旧前端 |
| GET /api/projects/{projectId}/tasks | tasks: projectId, taskKey, mode, issueId, title, requirementRef（不含路径）, requirementRevision, roundCount（未知为null）, historyAvailability |
| GET /api/projects/{projectId}/tasks/{taskKey} | 上述任务详情、legacyLinks、受控需求说明引用；需求版本不存在则 null |
| GET /api/projects/{projectId}/tasks/{taskKey}/rounds | rounds: roundId, entry, mode, sourceFormat, createdAt, originalRunConclusion, agentStatus, exportStatus, verificationCount, evidenceState |
| GET /api/projects/{projectId}/tasks/{taskKey}/rounds/{roundId} | 轮次详情、requirementSnapshotRef/null、baseCommit/null、regressionSha256/null、candidateSha256/null、分阶段报告引用、liveness、warnings |
| GET /api/projects/{projectId}/tasks/{taskKey}/rounds/{roundId}/verifications | verifications: verificationId（如verify-02）, sourceFormat, createdAt/null, rawStatus/null, independentlyVerified/null, reportRef, candidateSha256/null, integrityStatus |
| GET /api/projects/{projectId}/tasks/{taskKey}/rounds/{roundId}/artifacts/{artifactId} | 后端登记的报告/补丁/日志/摘要材料，kind、可用性、来源格式、脱敏/截断及分页标记；不存在/不可读不能返回伪造空成功 |

材料只允许服务端登记 ID，不暴露任意文件路径或完整环境秘密；沿用当前材料文本展示、分页/脱敏原则。当前已追加两个登记需求的实验启动 POST，具体契约见 history-api.md；尚不提供从指定准备继续或追加验收 POST。

### 现有 /api/tasks 兼容

- 原 GET /api/tasks 的50条列表字段、POST 创建及 /api/tasks/{32位ID}、preparation、execution、events/result/artifacts 都保持现状，旧收藏链接可用。本次不把它们无声改成实验路由。
- 首批新项目页走上面的项目内只读路由；旧网页任务按 `sourceFormat=web_task_v1` 适配为 ordinary 视图。其原 ID 保留为兼容身份，旧链接仍指原 API，不强造一个符合新时间格式的 roundId 写入磁盘。
- 需要统一导航时，适配层可给旧网页单次执行一个带来源前缀的不透明 round 导航键，显式映射原 taskId；只有实际声明/记录时才有该轮，未执行任务不能补造运行。该键不是新的正式 round.json ID。
- 不将实验状态直接挤入旧 executionStatus，也不把两个已知实验塞进旧32位任务接口。后续如需扩展旧列表，须另定兼容行为；首批只读接入不替换它。

## 6. 目录与后续写入

后续新需求沿用 `runs/<taskKey>/run_<ID>`；独立准备沿用 prepare_<ID>，旧归组沿用 legacy_<ID>。工作副本沿用 `experiments/<project>/workspaces/<issue或task-key>_<ID>_<role>`，已有 workspace_name 的命名与 verify 角色分配优先复用。详见实验目录文档，不新增 runs/tasks 作为目标体系。

项目注册表、需求身份、每轮快照与每轮结果职责分离；结果仍留原轮次。一次新的模型尝试分配新轮次和新副本，不能在旧 Agent 副本上重复跑来替代新轮次；同一 P 的 verify-02 不分配新模型轮次。

已产生的 runs/tasks 及 experiments/tasks 原位保留，只读兼容；本次不改旧网页写入位置，也不批准继续把它作为统一目标。写入切换与历史迁移分开：先实现并核对适配；未来新写入切换须明确兼容行为，已有材料移动必须另行授权，给显式路径映射、哈希/副本/环境前后检查并保留原证据，不覆盖旧结果。

## 7. 首批只读历史接入的验收边界

页面映射采纳前端会话提供的只读建议：项目列表→需求列表→轮次列表→轮次详情/verify详情。复用现有卡片、列表和结果布局；轮次详情分列准备、Agent、补丁和验收证据，verify详情展示对应一次验收报告。ordinary 展示固定最终检查，experiment 展示各次独立 G/R/P 验收，不共用一个含糊的“测试通过”标签。首批历史页面不带启动/重跑/追加验收按钮；旧网页 taskId 详情链接保留，不要求先重构整个前端。

首批实施只要求用户在网页完成：

1. 看到 browser-use、pydantic-ai 两个真实项目，分别进入 #5998、#10525 需求，而非演示数据。
2. 看见对应真实 run/prepare/legacy 轮次；轮次归属和显示结论能追溯到实际 round.json/原报告，缺失字段明确未提供。
3. 打开某轮的准备、Agent、P 导出与各次独立验收报告；区分模型结束、候选补丁与独立验收，verify-02 不显示为第二次模型运行。
4. 历史缺失、未关联或旧格式如实展示；旧网页真实任务及原收藏链接仍可读取。
5. 页面读取不创建副本、不启动测试/模型、不重写摘要或历史 JSON，不迁移文件。只做必要的直接页面/API读取核对，不以大规模重跑实验作为接入前置。

首批只读阶段不包含启动能力；后续增量已接入 #5998/#10525 的 `run_experiment` 启动、阶段与报告，检查见 experiment-start-verification.md。pydantic-ai 网页新轮次已有用户运行并独立验收通过的报告；browser-use 新入口未在本批核对真实轮次。`run_prepared` 和追加 `verify_experiment` 尚未接入网页。

## 8. 待实施时确定的细节

- task 元数据与需求修订号的具体存储格式、未来新写入切换时机；不影响先读旧轮次。
- 多项目同 taskKey 的全局唯一性处理（建议以 projectId/taskKey 组成身份；本机 runs 目录键碰撞时配置阶段拒绝，不隐式重命名旧目录）。
- 未关联材料的独立只读页面/路由和材料公开粒度；先保留未关联事实，不为UI整齐猜归属。
- 配置迁至 experiments/projects.json 与 runs/tasks 历史迁移需各自明确实施授权；本设计不执行迁移。
