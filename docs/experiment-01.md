# Experiment 01 — Repo tool argument routing regression

## 任务来源与产权界限
- Source: https://github.com/browser-use/browser-use/issues/5998
- Existing PR: https://github.com/browser-use/browser-use/pull/5999
- Status: **已有人提交修复**，我们的目标是训练与验证 Coding Agent 的修改闭环，而不是提交重复 PR。

## 实验问题
自定义 Tool 的 Python 函数签名若有 keyword-only 参数（`def f(text: str, *, count: int)`），注册时正确，但执行时可能将 `count` 当作位置参数调用而抛错。

可用的原始复现（来自 Issue；仅作实验输入）：

```python
import asyncio
from browser_use.tools.registry.service import Registry

async def main():
    registry = Registry()

    @registry.action("Repeat text")
    async def repeat(text: str, *, count: int = 2) -> str:
        return text * count

    print(await registry.execute_action("repeat", {"text": "hello", "count": 3}))

asyncio.run(main())
```

Expected: `hellohellohello`。Issue 报告在指定旧版上会出现将关键字参数当作位置参数的异常。

## Step 1 — 创建固定基线

PowerShell 示例：

```powershell
git clone https://github.com/browser-use/browser-use.git browser-use-sandbox
cd browser-use-sandbox
# Issue 中列明的旧版本参考 commit，必须先确认可 checkout
git checkout 7be96ed8bafa8dfe1eef228b59cf5c884b8b2431
# 依照仓库 CONTRIBUTING 指令安装对应依赖，再运行最小复现
```

建议使用新建 clone，并用 `git rev-parse HEAD` 记录完整 commit。不要在研究主仓库里直接让 Agent 修改代码。

## Step 2 — 第一个 Coding Agent 实验

- LLM 输入：Issue 原文、限制修改范围、要求增加 regression test。
- 可用工具：list_files / search_code / read_file / apply_patch / run_tests。
- 第一遍只给 read/search，检验能否找到 registry 调用路径。
- 第二遍开启补丁和测试工具，运行聚焦测试。

## Step 3 — 明确实验通过条件

1. 能在受控的历史版本复现 Issue（环境已安装正确）。
2. 能定位调用时按位置/关键字传参的代码路径，并提供最小补丁。
3. 至少添加一个 regression test，证明修复前失败、修复后成功。
4. 现有相关测试没有回归；记录旧版本 checkout 与新的改动 diff。
5. Agent 回答必须引用实际工具反馈；不能以 `finish` 当作测试通过。

## 要记录的数据

| 列 | 意义 |
|---|---|
| base_commit | 固定复现环境 |
| task_id | Issue 编号 |
| model | 具体模型；后续对比控制一致 |
| solved | 关键回归测试是否通过 |
| tests_passed | 已运行的测试通过情况，不等于整个项目无问题 |
| tool_steps | 工具执行次数 |
| patch_attempts | 生成补丁次数 |
| elapsed_sec | 端到端耗时（以后加入） |
| failure | 主要失败原因 |

## 不能声称的成果

当前还没有针对 browser-use 真实 repo 成功跑完 Agent 实验。离线 FakeModel 演示只验证本地执行基础设施通畅，不表明 Coding Agent 能自主修复真实 Bug。
