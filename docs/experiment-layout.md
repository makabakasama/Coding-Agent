# 实验目录

现有材料与新实验统一按上游项目分类，再分初始材料和工作副本：

```text
experiments/
  browser-use/
    inputs/
      source/                    # 上游源码仓库
      env/                       # 独立测试环境
      requirements/
        5998/                    # 问题说明、G、R 的位置与校验信息
    workspaces/                  # 每轮修复及独立验收的新工作副本
  pydantic-ai/
    inputs/
      source/
      env/
      tools-env/                 # uv 等安装工具
      requirements/
        10525/
    workspaces/
```

两个项目已有 source/env，pydantic-ai 另有 tools-env。不同依赖版本需要分别命名环境，不能覆盖已有环境。

当前两个入口将工作副本放入各项目的 `workspaces/`，按同一个轮次 ID 和用途命名。

## 轮次与角色命名

ID 为北京时间 `YYYYMMDD-HHMMSS_<8位ID>`，内部 UTC 时间字段保留。一次正式 `--run`
只分配一次 ID，使用原子目录预留防冲突；名称不包含成功、失败等结果。

```text
runs/pydantic-ai-10525/
  INDEX.md
  run_20261029-144226_7c920922/
    round.json                   # 入口、共享 ID 和整理说明
    summary.md                   # 根据实际材料生成的中文摘要
    experiment.json              # 正式 --run 的总报告
    repair/                      # 原准备/Agent/候选补丁的局部结构
    verify-01/                   # 首次独立验收
experiments/pydantic-ai/workspaces/
  10525_20261029-144226_7c920922_agent/
  10525_20261029-144226_7c920922_verify-01/
```

单独 `--prepare` 的根目录为 `prepare_<ID>`，随后通过 `--manifest` 运行模型也不改入口名称。
重复 `--verify` 在同轮追加 `verify-02` 等以及全新副本，不覆盖旧验收，不改原 `experiment.json`。
`summary.md` 和 Issue 根目录 `INDEX.md` 帮助找材料，结论以原始证据为准。

2026-10-29 已按原报告中的 manifest/verification 链接整理既有两项目：17 个记录目录归到
15 个轮次（含缺少记录的 legacy 轮次），17 个 linked worktree 已统一角色名。无关联材料的
副本标为 `legacy-unlinked`，不按时间猜测归属；缺失工作副本不补造。历史 JSON 内的旧 run_id 也不改写，round.json 提供统一导航 ID。旧格式独立报告仍保留
其 `manifest.json` 文件名和原字段，在摘要中单独注明，不冒充新协议。

整理报告：`runs/migrations/rounds_20261029T071037Z_656f1db8/report.json`；
迁前 map 原件、19 个 checkout 前后清单和 358 个历史文件的当前位置/哈希在同目录。
`scripts/rename-experiment-rounds.py` 是这次授权整理的一次性脚本，不应在已整理后盲目重跑。
source 和环境目录未搬动；browser-use seed 的 editable 元数据仅修正指向，并保留原件。

## 已完成的本机迁移

2026-10-28 已实际迁移两份 source、三套环境和 14 个 browser-use linked worktree。以下仅解释历史路径，不是当前执行命令：

| 项目 | 历史 source → 当前 source | 历史环境 → 当前环境 |
| --- | --- | --- |
| browser-use | `experiments/browser-use-source` → `experiments/browser-use/inputs/source` | `experiments/browser-use-env` → `experiments/browser-use/inputs/env` |
| pydantic-ai #10525 | `experiments/pydantic-ai-10525-source` → `experiments/pydantic-ai/inputs/source` | `experiments/pydantic-ai-10525-env` → `experiments/pydantic-ai/inputs/env` |

原根目录 `browser-use-5998*` 先移至 `browser-use/workspaces/<原目录名>`，随后按上文轮次与角色重命名；工具环境 `pydantic-ai-10525-tools` 已移至 `pydantic-ai/inputs/tools-env`。pydantic-ai 旧失败准备记录的工作副本从未创建，未补造副本或改写失败事实。

本机显式映射 `experiments/path-migrations.json` 由 `ExperimentSpec.path_migrations` 指定，支持旧扁平副本→分类目录旧名→当前角色名，以及旧 runs→新轮次目录的链式解析。仅允许项目根下 experiments/runs 两个明确区域，禁止映射其根、越界、循环、歧义和相似前缀误匹配，映射后继续 resolve 检查符号链接来源。旧入口 manifest 路径本身也先解析再读取，原 manifest、R、P、trace 哈希继续核验。历史记录保持字节不变；路径迁移不等于重新验收旧实验。

迁移报告在 `runs/migrations/20261028T084813Z_6fbfc043/report.json`，包含 16 个 checkout 的 HEAD、状态、diff 和 tracked/untracked 文件前后校验、三套环境依赖版本与启动器修复记录，以及 215 个历史文件哈希。修改前的 Scripts 和元数据在该目录的 `environment-originals/`。没有更新或安装依赖。

`scripts/migrate-experiment-layout.py` 是一次性迁移工具，已完成后不可盲目重跑；冲突会停止并保留计划/事件，不删除或覆盖目录。

需求目录中的说明指向受版本管理的入口和 `examples/fixtures/` 补丁，不另行维护一份可能漂移的权威补丁。`experiments/` 和 `runs/` 均被 Git 忽略，只代表本机留存。
