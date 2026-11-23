# 官方评分派生镜像缓存与镜像供应解耦

2026-11-19，按用户授权实施。目标：让同一道题的基线、A/B 和重试复用同一个已核验派生镜像，不再每次重新 build；并把镜像供应从评分启动器中解耦为独立模块。未启动真实模型、Docker 或官方评分。

## 现状问题

镜像准备原先内联在 `official_runner.py::prepare_image`：每次评分都重复「探测官方镜像 → 源码不符则 build 派生镜像 → 再探测」。派生镜像没有稳定标识，`images.get()` 无法命中，导致同题多次运行各 build 一次；只有镜像下载本身有 Docker 层缓存。A/B 源码内容相同，缺的只是可寻址的复用标识。

## 本次实现

- 新增 `repo_agent/evaluation/image_provider.py`，集中 `probe_image`、`restore_dockerfile` 和 `resolve_verified_image`；`official_runner.py` 删除这三个函数，仅保留评分编排、证据落盘和 `audit_score`，改从新模块导入 `resolve_verified_image`。
- 缓存键：`sha256(image_id + baseCommit + tree + RESTORE_RECIPE_VERSION)`，生成确定性本地 tag `coding-agent-verified:<digest>`。镜像摘要用拉取后的不可变 `image.id`，跨题用 `image.id` 隔离，不共享依赖环境；`RESTORE_RECIPE_VERSION` 在恢复脚本或源码核验逻辑变化时手动递增，使旧缓存失效。
- 命中路径先用该 tag 取镜像并做一次 probe，源码树仍匹配才复用（`image-cache.json` 记 `reused`）；未命中或探测失败一律降级到原完整路径，重新 build 后打 tag（`tagged`），任何缓存异常都不阻断评分。命中只省去 build 与重复探测，不跳过源码核验。
- `save` 下沉到 `container_lifecycle.py` 作为共享工具；`grader.py::execute_grader` 额外复制 `image_provider.py` 到评分目录，并在 `command.json` 登记引用；`launcher-inputs.json` 记录 `imageProviderSha256`；`benchmark.py::CODE_FILES` 加入新模块。
- 证据：`source-verification.json` 增加 `cache` 段（命中与否、tag、原因、异常类型），`image-cache.json` 单列缓存动作；原有 `sourceRestored`、`changedFields` 语义不变。

## 验证状态

- 静态检查：相关 7 个文件 `py_compile` 通过；导入核对确认新接口就位、旧 `prepare_image` 引用清零、`code_identity()` 覆盖 10 个文件且哈希齐全。
- 测试代码已更新：`tests/test_image_provider.py` 覆盖命中复用、未命中打 tag、陈旧缓存降级、探测异常降级、缓存键内容寻址；`tests/test_official_runner.py` 仅保留编排相关用例。**尚未运行测试**，等待用户按当次指示安排。
- 未运行真实 Docker、模型或官方评分，未证明缓存命中在真实镜像上的耗时收益，也未做重复运行的一致性实验。
- 因 `CODE_FILES` 改变，既有已注册 Benchmark 协议的 `code` 字段与新代码不一致，须重新 preflight/注册后才能用于正式入口。本次未重新注册。

## 下一步（待授权）

先按用户指定范围运行定向离线回归；通过后再在单题真实镜像上对比冷/热准备耗时，确认收益与评分可比性，最后决定是否重新注册协议。
