# Harness v0.6 RC1 审核报告

## 冻结范围

2026-11-19 已冻结用于 Benchmark 的运行源码。A 组来自三题 A/B 实验中实际归档的 v0.6 源码，B 组来自历史 P2 归档。两组各有 85 个源码文件，逐文件核对 Git 提交中的字节与原归档 SHA-256。提交保存在独立的本地快照仓库，并生成了可单独恢复的 Git bundle。工作台前端、宿主服务配置及已有实验材料不在这个源码范围内。项目原仓库 HEAD 仍为 `84fe99fd3dcc8dea3d100a20a43569b0054f85f3`，本地没有添加发布标签。

| 实验组 | 冻结提交 | Git tree |
| --- | --- | --- |
| A · v0.6 RC1 | `2f3b5139c6f39f781df7948121fa6352e63124dd` | `d8b974cc357d49096cbf636d935227d590937703` |
| B · 历史 P2 | `1c06d2c150ba3df8cd0e496814279377ce50492e` | `8f53235b6b1d3475835e431ffe332a58f378a208` |

本地归档索引为 `runs/engineering/v0.6-rc1-20261119/freeze.json`。包含两个 bundle 的 SHA-256、运行依赖清单和对应配置。当前 Provider 的主机名仅留在本地索引，历史轨迹可确认适配器、模型和配置档，无法确认历史 endpoint 主机名或网关内部模型版本。

| 项目 | 固定值 |
| --- | --- |
| 历史运行的模型与 Provider | `gpt-6.1-sol`，OpenAI-compatible 网关，`gpt` 配置档，`temperature=0`。后续批次使用启动时项目根目录的 `.env`。 |
| Token Budget | 每题每组 500,000 累计计账 token，`auto` tokenizer，128,000 context，8,192 普通输出，1,024 收尾输出和 128 收尾余量 |
| 请求与终止 | 50 次决策，1,800 秒软超时，单 HTTP 请求 90 秒，传输重试 0 |
| 运行环境 | 现有 `coding-agent` Conda，Python 3.12.14，A/B 归档依赖清单 SHA-256 相同。标准权限提示 SHA-256 为 `456ab9547fb929366969592bc8f1339640370f06252db6f275dee2e6365eb376` |
| 开发与评分 | 每题固定开发镜像，开发检查 90 秒。SWE-bench 官方评分器 5.0.2，600 秒评分超时，容器串行，每容器 1 GiB |

预算为客户端计账准入。Provider 未报告用量时保守估算，实耗上界仍取决于网关。

## 五道重点任务

以下每题取指定的最新有效真实记录。三题 A/B 为同一批数据，另外两题属于历史诊断轮次。所有列出的模型试验均有完整的 Provider 用量回执。

| 任务 | 官方目标 / 控制 | Provider Token | 模型自报与开发验收 | 已记录的异常或限制 |
| --- | --- | ---: | --- | --- |
| Django 11019 · P2.2 | 2/16、58/58，未解决 | 467,668 | `incomplete`，`needs_review` | 原生测试发现 tuple/list 回归，复验恢复；独立资源链场景未实际验证。第 24 次紧凑收尾。 |
| SymPy 14817 · 共享 Harness 真实轮 | 0/1、111/111，未解决 | 462,565 | `incomplete`，`developer_check_failed` | 原生检查的 3 个异常在基线重现；候选临时断言失败。后续 P1/P2 重放提供了公开证据。 |
| Django 15498 · A / B | 两组 1/1、25/25，均解决 | 321,076 / 379,528 | A `incomplete`、`needs_review`；B `completed`、开发层 `needs_review` | 两组独立应用补丁和评分通过。 |
| SymPy 18835 · A / B | 两组 1/1、41/41，均解决 | 190,029 / 341,912 | A `completed`、`needs_review`；B `completed`、开发层 `needs_review` | 两组独立应用补丁和评分通过。 |
| pytest 5495 · A / B | 两组 2/2、86/86，均解决 | 446,150 / 455,204 | 两组自报 `incomplete`；A 验收 `needs_review`，两组开发层均为 `verification_incomplete` | 首次基线评分落盘 GBK 编码错误；通过 UTF-8 重试后启动两个模型轮次。 |

以上执行均为 `agent_reported_finished`。B 组使用早期交付协议，表中的开发层等级取自 `developerVerification.level`。官方通过采用评分器的 `resolved`，补丁应用和控制测试均已核对。

三道新题 A 与 B 各为 **3/3**。A 组 957,255 token，B 组 1,176,644 token，样本内相差 219,389 token（**18.6%**）。历史诊断两题为 **0/2**。按表中每题取一份 A 或诊断记录，描述性合计为 **3/5，1,887,488 token**。它们跨版本、样本选择和运行时点，合计只供回顾。真实模型单次运行的变化、跨题节省和官方解决率提升仍需下一阶段衡量。三题观察到的两个 A 组 `incomplete` 仍被官方判为通过，也说明模型自报与外部验收各自有独立用途。

依据见 [三题 A/B 实验](swe-lite-ab-3-task-results-20261119.md)、[Django 11019 复测](p2-2-e2e-django-20261118.md) 与 [Django / SymPy 共享 Harness 复测](shared-harness-real-retest-20261118.md)。

## 审核结论

Benchmark 源码候选 **已冻结，可用于下一阶段预检**。完整工作台发布仍待前端全量和专用 PostgreSQL 集成验收。模型、样本资格和官方评分等控制条件见 [下一阶段 Benchmark 协议](benchmark-rc1-protocol-20261119.md)。宿主空间变化与可执行的资源门槛见 [磁盘单独审计](benchmark-host-storage-20261119.md)。本轮未启动新的模型运行或官方评分。
