# Benchmark 宿主磁盘审计

## 历史变化与本机核查

11 月 18 日共享 Harness 复测前后 C 盘少约 6.08 GiB，P2.2 单题复测曾少约 4.74 GiB。11 月 19 日六次新题对照完成时，C 盘较预检多约 0.16 GiB，D 盘少约 2.31 GiB，E 盘少约 0.49 GiB。这些为端点差额，评测期间的峰值及历史逐文件分配量未记录。 [共享 Harness 复测](shared-harness-real-retest-20261118.md) · [P2.2 实测](p2-2-e2e-django-20261118.md) · [六次对照](swe-lite-ab-3-task-results-20261119.md)

| 当前对象 | 核查结果 | 可作出的判断 |
| --- | --- | --- |
| Docker Desktop 主数据盘 | `<Docker 数据目录>/docker_data.vhdx`，文件长度约 43.31 GiB。WSL 登记的 `docker-desktop` 系统盘也在 D 盘。 | 主数据盘目前落在 D 盘。镜像与构建缓存仍占用 D 盘，即使评测容器已经删除。 |
| 旧 Docker 系统盘 | C 盘旧目录下发现约 96 MiB 的 `ext4.vhdx`，最后写入 11 月 17 日。 | 它的现存长度和时间无法解释 11 月 18 日多 GiB 的变化。 |
| Ubuntu 的 WSL 数据盘 | WSL 登记在 C 盘，文件长约 4.42 GiB，当前仍有写入。 | 它是 C 盘需要监测的另一项，当前大小无法反推评测期间的增长。 |
| Windows 分页和 WSL swap | `%SystemDrive%/pagefile.sys` 文件约 2.98 GiB，分页用量峰值指标为 4,489 MiB。`.wslconfig` 设置内存 8 GB、swap 2 GB，未指定 `swapFile`。本次在默认临时目录未取到正使用的 swap 文件证据。 | 分页峰值缺少历史时间对齐。Microsoft 文档给出的 WSL 默认交换文件路径位于 `%TEMP%`，仅凭默认配置无法判断当时实际增长。 [Microsoft WSL 配置](https://learn.microsoft.com/windows/wsl/wsl-config) |
| 工作副本、归档和转储 | 项目轨迹和工作副本在 E 盘，开发输出归档在 D 盘。已知 Windows 专用转储文件在 D 盘。 | 归档与源码副本的增长可按盘单列。文件长度与磁盘已分配空间分别统计。 |

**当前结论**是 Docker 迁移已由配置、WSL 注册路径和实际文件共同确认。C 盘历史波动的具体归因仍缺同时段证据，现有 Ubuntu 虚拟盘和分页读数只提供了后续观测对象。Docker 官方也说明可配置磁盘镜像位置，核查时应以配置路径为准。 [Docker WSL 2 存储位置](https://docs.docker.com/desktop/features/wsl/)

## 批量评测的资源门槛

下一轮先跑三道不同难度的无模型基线预检，记录镜像拉取前、拉取后、基线评分前后，以及镜像复用时的峰值。对每题在阶段切换与运行中每 30–60 秒记录 C/D/E 空间、Docker VHD、Ubuntu VHD、Windows 分页文件大小与实际用量、WSL 和容器内存。同步记录现存容器数和清理回执，按题保存到本地记录。一次性 VHD 文件长度只用于辨认对象，盘面差额使用实际卷空间。

建议首批工程水位为 **C ≥25 GiB、D ≥45 GiB、E ≥25 GiB**，并要求预检后剩余镜像的保守估算落在各盘水位之上。这是根据已有约 6.1 GiB 的 C 盘波动和当前机器容量设定的首批水位，由无模型试跑再校验。若达到低水位，暂停拉取新镜像和发起新模型试验，保留当前任务材料及已有成绩。评分与开发检查沿用单容器、1 GiB、WSL 8 GB，运行后核对容器删除。当前机器约 C 37.5 GiB、D 93.4 GiB、E 46.0 GiB 可用；镜像峰值实测和正式入批门槛仍待无模型预检。

本轮磁盘操作限于只读核查。WSL、Docker、分页和系统文件的配置保持本轮检查时的状态。
