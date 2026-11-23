# Evaluation 容器内开发检查

后续策略更新：启用该通道时，现已增加自测提醒与当前版本通过记录的完成校验，新增部分尚未运行测试；见 [策略实施](evaluation-check-policy-20261116.md)。下文“未执行检查仍能完成”仅为初版历史行为。

后续验收：定向检查9 passed/9 subtests及真实容器检查通过，stdout/stderr处理缺口已修复。模型重跑未使用检查工具并明确报告未完成，官方仍未通过；见 [最新结果](swe-lite-developer-checks-result-20261116.md)。下文尚未测试等为历史实施记录。

日期：2026-11-16。用户确认继续执行此前方案，本批接入代码并做静态核对，**未运行测试、容器或模型**。此前官方失败结果不改写。

## 当前实现

默认仍禁用Agent测试；显式 run-agent --agent-tests 并提供 --check-image 后，启用Evaluation专用检查通道。--check-image必须为本机已有的不可变sha256镜像ID，每次检查重新核对其G、tree和干净工作区；不自动拉取或恢复镜像，缺少版本/依赖时返回环境失败。

模型通过原有工具完成：

1. create_file(path=".agent-checks/check_name.py", content=...)：创建临时Python检查。
2. read_file / replace_lines：读取与修改检查脚本。
3. run_tests(target=".agent-checks/check_name.py")：在容器执行脚本，回传退出码、输出摘要与日志入口。
4. 修改业务代码，再次run_tests，直到交付或明确报告未完成。

.agent-checks/是虚拟路径，真实文件保存在本轮runs目录的agent-checks/，不写入Agent业务副本、不进入candidate.patch。临时脚本修改使用快照核对后直接写文件，不让Git发现外层Coding-Agent仓库。每次执行另存检查脚本和当前业务补丁的不可变快照，保留历次失败。

也支持运行固定G中已有的测试文件。Sympy调用该版本原生bin/test -C --verbose，不假设旧环境能运行宿主pytest；其他SWE Python仓库使用容器pytest，依赖必须已经存在。Sympy首批仅支持文件目标，不支持目录、整套测试或函数选择器。原测试、测试配置和官方冻结R仍不可修改；检查容器不加入隐藏R或gold，不给模型官方评分反馈自动续跑。

## 执行与记录

专用评分解释器（默认当前Conda，可用--check-python指定）通过Docker SDK创建新检查容器。仅拷入当前业务补丁与临时检查，不挂载宿主目录、不传入宿主密钥环境、关闭网络、限制1 CPU/2GiB内存/256 PID；测试默认90秒，可用--check-timeout设置1–300秒。不是完整安全审计或通用沙箱保证。

每次从核验过的G开始应用当前业务补丁，再运行检查。临时脚本使用容器testbed Python和/testbed源码；Sympy额外核对实际导入位置。应用补丁后仅在专用容器移除/testbed/.git，防止检查脚本读取镜像中G之后的历史；依赖Git元数据的测试可能不适用。宿主仓库和历史材料不变。

保存请求、镜像/容器ID、G/tree、实际解释器/导入来源、补丁和检查哈希、执行命令、退出码、输出及阶段错误。模型收到最多12000字符摘要。主机输出保存上限1MB，超出则标记截断；完整输出仍在保留的容器中。容器正常执行后停止保留，中断后状态需检查，不承诺自动清理。

原仓库测试未执行任何用例、全部跳过、超时或环境失败均不记为通过。模型脚本exit 0只说明该脚本退出成功，不保证有断言或预期正确，始终标为开发检查，不产生官方Grade。

## 收尾证据

临时测试写入只增加check_revision，不增加业务edit_revision，不能拿“只写了测试”通过业务修改完成检查。业务或临时测试改变后，之前的通过证据失效；即使没有显式计划，最后一次检查失败也拒绝completed/already_satisfied，允许明确以incomplete/blocked结束。

没有运行过检查时，finish仍只是模型交付声明，不自动强制完成测试。官方评分、开发检查和模型finished继续分开保存。

## 使用入口（示例，不代表授权或已执行）

先准备全新任务和外部已验证基线，并从已核验镜像的source-verification.json取imageId：

```powershell
$checkImage = (Get-Content '<verified-source-verification.json>' -Raw | ConvertFrom-Json).imageId
python -m repo_agent.evaluation run-agent --run-dir '<new-run>' --baseline-report '<baseline-report.json>' --max-steps 50 --max-total-tokens 500000 --max-seconds 1800 --output-tokens 8192 --agent-tests --check-image $checkImage --check-timeout 90
```

这是新的harness配置，真实模型调用需单独授权。不能在旧失败轮续写，也不能把已暴露官方断言的本题重跑当新的独立能力样本。

## 验证边界

已新增离线回归代码：外部临时文件/补丁隔离、拒绝越界与非测试目标、检查快照保留、失败反馈→修改→重测的同循环、临时测试不算业务实现、失败检查不准completed、原生runner无用例/失败/超时分类。上述用例尚未运行。

本批只完成AST解析与静态差异核对，没有启动Docker、测试或Provider，也没有重跑上次模型。下一步按明确测试授权验收“容器检查失败→模型/脚本化决策修改→再次检查通过”，之后再安排新配置的真实任务尝试。
