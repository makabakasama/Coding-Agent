# 派生镜像缓存改动交接记录（2026-11-19）

> 用途：交接本次「官方评分镜像供应解耦 + 派生镜像缓存」改动。本文自包含，接手人无需回看对话。
> 严格区分：已实现 / 静态检查 / 未运行测试 / 未验证收益 / 已造成的实际故障。

## 1. 结论先行

- **改了什么**：把镜像准备逻辑从 `official_runner.py` 抽成独立模块 `image_provider.py`，并加入按「镜像 ID + 目标提交 + tree + 恢复脚本版本」寻址的派生镜像缓存。命中时只做一次源码核验即复用，不再重复 build；缓存任何异常都降级到原完整路径。
- **验证到哪**：只做了静态检查（编译、导入、接口、哈希覆盖）。**测试未运行，真实 Docker 未跑，缓存收益未验证。**
- **重要事故**：这次改动于 16:02–16:05 落盘，正值一个正式 A/B 批次运行中（15:xx 启动、16:26 结束）。改动导致该批次 **5 次评分以 `ModuleNotFoundError: No module named 'image_provider'` 退出、未评分**。详见第 5 节。这是本次改动的实际后果，交接时必须知晓。
- **当前仓库状态**：改动均在工作区、未提交；`CODE_FILES` 已变化，既有已注册 Benchmark 协议的 `code` 字段与新代码不一致，正式使用前必须重新 preflight/注册。

## 2. 背景与目标

用户观察正式测评 Docker 复用率低：同一道题的基线、A/B、重试都各自完整跑一遍镜像准备（取官方镜像 → 探测源码 → 不一致就 build 派生镜像 → 再探测）。派生镜像 build 时没有 tag，是匿名 dangling image，`images.get()` 命不中，所以每次都重新 build + 两次 probe；只有镜像下载有 Docker 层缓存。

目标：按不可变键缓存已核验的派生镜像，让同题各臂/重试复用；同时不削弱"可核验"约束（复用也要 probe 复核源码）。

## 3. 改动清单

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `repo_agent/evaluation/image_provider.py` | 新增 | 迁入 `probe_image` / `restore_dockerfile` / `prepare_image`（后者重构为 `resolve_verified_image`），新增缓存键 `cache_tag` 与打标/构建/降级逻辑 |
| `repo_agent/evaluation/official_runner.py` | 修改 | 删除上述三个函数定义，改为 `from image_provider import resolve_verified_image`；`_main` 调用新函数；`launcher-inputs.json` 增记 `imageProviderSha256` |
| `repo_agent/evaluation/container_lifecycle.py` | 修改 | `save()` 下沉至此，供两个模块复用（复制运行时同目录可见） |
| `repo_agent/evaluation/grader.py` | 修改 | `execute_grader` 复制清单加入 `image_provider.py`；`command.json` 增记 `imageProvider` 引用 |
| `repo_agent/evaluation/benchmark.py` | 修改 | `CODE_FILES` 加入 `image_provider.py`（10 个文件纳入哈希冻结） |
| `tests/test_image_provider.py` | 新增 | 11 个离线用例：缓存命中、未命中打标、命中核验失败降级、键稳定性等 |
| `tests/test_official_runner.py` | 重写 | 精简为 1 个启动器编排用例（Linux LF 包装） |
| `docs/v0.6/derived-image-cache-20261119.md` | 新增 | 实施记录（当时写于改动落盘后） |
| `docs/status.md` | 修改 | 顶部当前结论加一条并链接实施记录 |

## 4. 技术方案要点

- **缓存键**：`cache_tag(image_id, commit, tree) = sha256(json{image, baseCommit, tree, recipe:RESTORE_RECIPE_VERSION})`，本地 tag 形如 `coding-agent-verified:<digest>`。`RESTORE_RECIPE_VERSION = 'v1'`，改 `restore_dockerfile` 或源码核验逻辑时必须递增，否则旧镜像会在新规则下被误复用。
- **命中路径**：`images.get(tag)` 取到 → 临时容器 probe 源码（证据文件名用 `cache-*`，避免与完整路径的 `effective-*` 独占写冲突）→ 仍匹配 `commit`/`tree`/clean 才复用，并写 `image-cache.json`（`status=reused`）。
- **未命中 / 异常路径**：`ImageNotFound`→`reason=cold`；probe/查询异常→`reason=probe_failed`；打标失败→`status=tag_failed`。一律降级到原完整路径（probe 原镜像 → 必要时 build → probe 复核 → 打标），任何缓存故障都不阻断评分。
- **跨题隔离**：键含 `image.id`，不同题不共用依赖环境，保持官方评分可比性。
- **证据不打折**：命中与未命中都写 `source-verification.json`（含 `cache` 字段），复用也留 probe 记录。
- **复制运行适配**：模块用 `__package__` 双分支导入，同时支持包内导入与评分目录内的顶层导入；grader 复制清单与 `CODE_FILES` 已同步。

## 5. 事故：改动造成 5 次正式评分未评分（交接重点）

### 5.1 事实与时间线（文件 mtime 交叉核对）

| 时间 | 事件 |
| --- | --- |
| 15:46 / 15:50 | pytest-5221 双边评分，用旧 launcher（内联 `prepare_image`），正常出分（2 次官方未通过） |
| 15:52–16:01 | 批次继续跑 django-15400、django-16229 B 等 |
| **16:02–16:05** | **本次改动落盘**（container_lifecycle 16:02:16、official_runner 16:03:45、grader 16:03:59、benchmark 16:04:05、image_provider 16:04:21） |
| 16:04:40 | django-16229 A 评分失败：缺模块 |
| 16:08:51 / 16:10:53 | sympy-24213 A/B 评分失败：缺模块 |
| 16:13:46 / 16:15:29 | scikit-learn-12471 A/B 评分失败：缺模块 |
| 16:26:02 | 批次总结落盘，正式 A/B 验收 FAIL |

### 5.2 直接证据

失败评分目录 `runs/evaluation/swe-bench/run_20261119-160140_c16fafee/official/verify_20261119-160440_2447abe1/`：

- 只有 `official-runner.py` 和 `container_lifecycle.py`，**没有 `image_provider.py`**；
- `stderr.log` 原文：
  ```
  File "...\verify_20261119-160440_2447abe1\official-runner.py", line 24, in <module>
      from image_provider import resolve_verified_image
  ModuleNotFoundError: No module named 'image_provider'
  ```
- 复制进去的 `official-runner.py` 哈希 `38a04f...`（7834 字节）与 `command.json` 声明一致 = 本次改动后的新版；`command.json` 却只有 `launcher` + `containerLifecycle` 两个文件引用，无 `imageProvider` 字段。

### 5.3 机制

批次进程在 15:xx 启动时把**旧 grader.py**（复制清单只有两个文件）加载进内存。改动落盘后，进程每次评分仍按旧清单从工作区复制：读到了**新的 `official-runner.py`**（顶层 import `image_provider`），却没复制新增的第三个文件。于是在 run-local 解释器里 import 失败、评分进程退出。command.json 由旧 grader 生成、只校验被复制的两个文件哈希，所以"启动时校验了评分器源码"没能拦住它。即报告所述"版本漂移：加载到进程的打包逻辑与后续复制的脚本不一致"。

### 5.4 影响范围（哪些与本次改动无关）

14 次运行中，**只有 5 次与本次改动有关**（上表 5 次 `official_score_not_available` 且补丁非空）。其余：

- 2 次 django-15400：`execution_not_verified`，`Git operation failed (rev-parse); exit code 128`（Git 所有权，与模块无关）；
- 5 次 `no_candidate_patch`：sympy-17630 A/B、xarray-4493 A/B、django-16229 B，候选补丁 0 字节（模型未产出补丁）；
- 3 题未启动：flask-4045、matplotlib-26011（环境预检阻断）、sphinx-8627（基线证据不足）。

唯一出分的 pytest-5221 恰好跑在改动前，反证旧组件正常。批次报告是批次自身在改动后（16:26）写的，非本次改动产出。

## 6. 当前状态与风险

- 所有改动在工作区未提交（`git status` 显示 evaluation/ 整体为未跟踪目录）。
- `CODE_FILES` 从 9 → 10 个文件：`benchmark.py::code_identity()` 已覆盖新模块且哈希齐全；但既有已注册协议的 `code` 字段仍是旧值，直接复用会被 `registered()` 拒绝。
- 官方批次 FAIL，7 份非空补丁未补评，Docker 容器无残留（批次已核对）。
- 未运行任何测试；未运行真实 Docker；缓存命中收益、与旧组件结果一致性均未验证。

## 7. 下一步（需决策，两个方向）

报告 `docs/v0.6/benchmark-production-results-20261119.md` 已给出恢复范围。补评只需现存 7 份非空补丁，模型无需重跑。两个方向：

1. **回滚到旧组件补评（建议）**：临时撤下本次改动或让评分目录用回已注册的两文件 launcher，用与 pytest-5221 相同的、已验证的评分条件补评 7 份补丁。优点：可比性最好、最快恢复成绩、不变更评分条件。
2. **用新组件补评**：保留改动，但须先重新 preflight/注册协议（`CODE_FILES` 新哈希），并用新组件重评 pytest-5221，否则 A/B 两组不在同一评分条件下、不能计入配对。

无论哪个方向，Git 所有权问题（django-15400）都要单独给限定路径授权 `safe.directory`，空补丁的 5 次无法补评。

## 8. 交接须知

- 不要在批次运行期间改动 `evaluation/` 下任何被 `CODE_FILES` 冻结的文件；如确需改，先停批次或确认无运行中的评分。
- 改动 `restore_dockerfile` / 源码核验逻辑时递增 `RESTORE_RECIPE_VERSION`。
- 缓存命中仍须 probe 核验，不要为省时而跳过；缓存操作失败必须降级，不得阻断评分。
- 本报告只记录事实，未执行补评、未回滚、未提交、未运行测试。是否按方向 1 回滚/补评，待接手人决定后执行。

## 附：关键路径

- 改动代码：`repo_agent/evaluation/{image_provider,official_runner,container_lifecycle,grader,benchmark}.py`
- 本批次目录：`runs/engineering/batch-prod-20261119/`
- 批次报告：`docs/v0.6/benchmark-production-results-20261119.md`
- 实施记录：`docs/v0.6/derived-image-cache-20261119.md`
- 缺模块 stderr 实例：`runs/evaluation/swe-bench/run_20261119-160140_c16fafee/official/verify_20261119-160440_2447abe1/stderr.log`
