# 文件定位与搜索反馈

V0.5 第二批为现有文件工具增加分页、范围搜索和覆盖信息。调用通过原生工具协议进入 RepoTools，并沿用任务的可读范围及运行材料排除规则。

## 工具接口

```python
list_files(subdir=".", cursor="", limit=500)
search_code(query, path=".", cursor="", limit=50)
```

`list_files` 的 limit 为 1–500，`search_code` 为 1–50。搜索采用大小写不敏感的字面子串匹配，path 接受一个目录或文件。省略新参数的旧调用仍可使用。

列表保留 files、returned、limit、scope、supported_extensions，新增 complete、has_more、next_cursor、stop_reason 和 skipped_counts。完整性限定于声明的可见文本类型和策略范围；visibility 列明排除目录、敏感文件、链接及各任务模式的过滤规则。

搜索保留 matches 和 truncated，增加以下反馈。

| 字段 | 含义 |
| --- | --- |
| scan_complete | 所选可见范围已经处理完毕，且没有影响覆盖结论的跳过 |
| matched_lines_total | 同一次查询各页累计发现的匹配行数 |
| scanned_files | 本页完成扫描的文件数 |
| skipped_counts | 本次查询累计的 too_large、read_errors、enumeration_errors 计数 |
| has_more / next_cursor | 是否有剩余工作以及下一页游标 |
| stop_reason | 本页停止原因，例如匹配数、扫描文件数或输出预算耗尽 |
| matches[].text_truncated | 该匹配行的显示文本是否截短 |

matches 仅包含本页。只有 scan_complete=true 且 matched_lines_total=0，才能报告所选可见范围未发现匹配。Agent 可缩小 path 或继续读取下一页，长行上下文通过 read_file 获取。

## 续查与版本

has_more=true 时，使用 next_cursor 和相同工具、查询及范围继续。游标属于当前 RepoTools 实例，最多保留最近 32 个续查状态；过期、未知或跨查询游标返回错误，需重新查询。页大小可以在合法范围内调整。

续查核对候选路径，以及此前搜索已读取文件的 SHA256。新增、删除、重命名候选或修改已读取文件导致续查失效时，重新开始查询。结果属于有版本检查的逐次读取；读取之后的外部变化仍需由后续读取核对。文件列表和搜索均需枚举候选路径，目录枚举成本随可见文件数量增长。

搜索每页最多处理 1500 个候选文件；同一文件内未返回的匹配可通过游标继续。大于 700000 字节的文件会记录跳过，读取和遍历错误也会反馈覆盖缺口。策略排除的文件通过 scope 和过滤规则界定。

## 权限与记录

搜索 path 只能缩小原有可读范围。allowedPaths 继续用于编辑授权，阅读测试和配置的既有规则按任务策略执行。单文件任务保持单文件读取范围。Git direct 沿用 Git 可见文件枚举，普通目录和 worktree 沿用对应路径过滤。

原始工具观察保留完整分页反馈；公开事件与界面展示范围、完整性、停止原因、扫描数、累计匹配和跳过计数。查询游标保留在工具对话中。

## 检查入口

```powershell
.venv/Scripts/python.exe -m pytest tests/test_code_discovery.py tests/test_provider.py tests/test_tools.py -q
node --test frontend/tests/task-ui.test.cjs
```

工程检查使用临时目录、合成仓库和脚本模型。真实 Provider 的代码定位效果需要单独的运行证据。

2026-11-10 的最终定向回归覆盖定位、协议、原有编辑及撤回链路，结果为 112 passed、1 skipped、146 subtests passed；前端检查 65 passed。Windows junction 禁止下钻检查通过，POSIX FIFO 用例因本机缺少对应接口跳过。

第二批首次完整后端运行另返回 292 passed、2 failed、16 skipped，两个失败发生于 HTTP 错误响应子用例的 Windows 连接中止。首次失败与复测输出保存在本机 runs/engineering/v05-code-discovery-20261110/。

后续 HTTP-01 确认并修复早期拒绝后未读数据的关闭处置缺口。当时完整后端为 299 passed、16 skipped、259 subtests passed，前端 65 passed；原始请求场景修复前 499/500 通过、修复后 500/500。源码、补丁、诊断过程及归因边界保存在本机 runs/engineering/http01-20261110/。后续第三批修改的检查另见执行上下文与步骤进度记录。
