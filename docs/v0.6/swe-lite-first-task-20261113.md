# 首道 SWE Lite 任务准备

日期：2026-11-13（北京时间）。本轮准备材料并查询环境，没有运行官方仓库测试、模型或评分，也没有拉取镜像、启动容器、安装评分器或重启 Docker。

最新进展：后续经用户授权已完成官方 G+R 基线，目标失败 1 项、控制通过 120 项，模型调用 0，详见 [基线验收](swe-lite-baseline-20261113.md)。以下为准备阶段原始记录，保留当时未执行与环境状态。

## 已完成

| 项目 | 本轮记录 |
| --- | --- |
| 任务 | `sympy__sympy-15308`，矩阵表达式的 LaTeX 打印 |
| 数据版本 | SWE-bench Lite test，revision `b0dde1093fe417d83b7184254edf8199c1f0dff5` |
| 源仓库 G | sympy/sympy，commit `fb59d703e6863ed803c98177b59197b5513332e9` |
| 冻结 R | 官方 test_patch 已原样保存并记录 SHA256；期望目标失败 1 项、控制通过 120 项，尚未执行验证 |
| 材料 | 官方记录、测试清单、R、参考补丁分别保存于受控 inputs；参考补丁未作人工阅读，不进入 Agent 工作区 |
| 工作副本 | 通过现有 prepare 入口创建；源仓库和新副本的 HEAD 均与 G 一致，状态均干净 |
| 镜像 | `swebench/sweb.eval.x86_64.sympy_1776_sympy-15308:latest`，Docker Hub 元数据压缩体积 1,074,716,689 字节（约 1.07GB）；未拉取，解压/运行峰值未知 |
| digest | manifest `sha256:680c0996a3017e221a5c45aa535faf1dab9b0ef38a6ee1863ef894d079f94934`；linux/amd64 子镜像 `sha256:13edc0fe53d44216af865ac94a327dc899f01555f440ef4abb9951b986c283a1` |

选择依据为固定行号 250 对应的纯 Python 打印任务、环境成本和小规模接入需求，未按 Agent 成功结果挑题。最初官方 filter 查询超时，没有将无响应解释为任务不存在；改用 rows API 获取固定行，并在导出前后核对数据集 HEAD 均为上述 revision。当前保存的是官方 rows API 导出及哈希，未额外下载/解析 Parquet 校验提取等价性，该来源边界保留在 preparation.json。

源仓库只抓取固定 commit 的浅历史，未下载完整历史；源树及 Git 数据合计约 36.9MB。实验副本仍是文件隔离，不是执行沙箱。

## 基线计划

已生成待执行的官方基线命令和独立 baseline-predictions.jsonl。官方 runner 可能直接跳过空补丁，因此准备一份同一行删除/加入文本完全相同的 identity patch；它不改变业务代码，已通过 `git apply --check` 静态核对。尚未应用该补丁或执行测试，不能把静态检查写成缺陷已复现。

后续只对本题调用官方评分器，检查 G+R 下预定目标失败与控制通过，并保存实际解释器、镜像身份和逐题报告；不得把缺依赖、导入错误或环境失败当作目标 Bug 复现。该步骤通过前不启动 Agent。

## 本轮发现与修正

首次调用真实 prepare 时，公共脱敏器把 `GIT_CONFIG_KEY_n` 的配置项名称误当密钥，官方记录中的 eval_script 因包含该名称被拒绝。首次失败目录保留，未放宽 prepare 对真实密钥的拒绝。

修正脱敏器：Git KEY_n 被识别为配置名称；真正的 API 密钥及指向敏感 Git 配置的 VALUE_n 仍受保护。新增 2 个定向用例，与既有适配器 11 个用例联合回归，结果 **13 passed，10.07 秒**。这是适配器离线回归，不是官方仓库测试或模型结果。修正后用全新的 run ID 成功准备副本，未覆盖失败轮。

## 环境最新状态

初始只读查询曾成功连接 Docker 29.9.2 的 linux/x86_64 引擎，内存约 15.4GiB。后续最终复核中，desktop-linux 和 default 的本地 engine 管道均不存在，当前连接不可用。先后结果分别留档，不推断已卸载，也没有主动重启。

最后一次 image inspect 因引擎不可达失败，不能据此证明本机没有该镜像。远程镜像大小为压缩传输元数据，不等于本机运行峰值，也不作为整套 benchmark 的资源要求。

## 本机材料入口

- 准备：`runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/`，含输入哈希、环境、镜像元数据、基线命令、失败记录及回归输出。
- [准备摘要](../../runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/summary.md) / [源与副本核对](../../runs/evaluation/swe-bench/prepare_20261113-142837_e4908484/verification.json)
- 成功准备轮：`runs/evaluation/swe-bench/run_20261113-144440_f069541c/`；Agent 未执行，baselineTests 为 not_run。
- 首次失败轮：`runs/evaluation/swe-bench/run_20261113-144058_2e577424/`，保留误报原因。
- inputs 与工作副本按 `experiments/sympy__sympy/` 分类，原始材料不复制进公开文档。

这些是被 Git 忽略的本机材料，不表示远端备份。下一步恢复/确认 Docker 连接、准备固定官方评分依赖与单题镜像，再按明确测试授权执行基线；真实模型调用及费用预算仍未授权。
