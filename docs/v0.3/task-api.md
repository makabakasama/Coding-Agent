# M1 任务保存与查询接口契约

## 2026-11-02 增量：真实任务列表

`GET /api/tasks` 返回 `200 {"tasks":[{"taskId":"完整32位ID","createdAt":"UTC时间","projectId":"项目编号","current":"当前行为正文","executionStatus":"not_started","resultAvailable":false}]}`。按 createdAt 最新优先，最多50项，无分页/搜索；原 POST 保存保持不变。

executionStatus 复用执行状态，未启动为 not_started，已有声明但缺失/异常的执行记录为 unknown；列表不触发准备、执行或可启动性检查。resultAvailable 只表示结果文件存在，不代表内容有效或检查成功。无任务返回空数组；无正式 task.json 的未完成创建不计入列表，正式任务记录损坏/存储错误沿用既有错误响应，不伪装为空列表。查询不会修改历史任务记录。

契约版本：`schemaVersion: 1`。日期：2026-10-31。

状态：M1 接口已实现，前端已接入。范围与分工见[开发计划](README.md)，本地启动见[运行说明](runbook.md)，实际验证与限制见[验收记录](m1-verification.md)。字段调整先更新本文，再同步前后端和验收用例。

## 1. 通用约定

- API 前缀为 `/api`，JSON 属性统一 camelCase，UTF-8 编码。
- 服务与前端同源，仅绑定 `127.0.0.1`；写请求须使用 JSON，拒绝不匹配的 Origin（若携带）及跨站请求，不启用通配 CORS。
- POST 请求体上限 64 KiB；未知字段、错误类型、重复 JSON 键拒绝。布尔字段不接受字符串 `"true"` 或数字。
- ID 使用 32 位小写随机十六进制串。地址和请求中不携带本机仓库路径、解释器路径或密钥。
- 时间由后端生成，RFC 3339 UTC 格式，例如 `2026-10-31T02:00:00Z`；前端按用户时区展示。
- 响应使用 `Cache-Control: no-store`。错误不返回堆栈、本机绝对路径或底层异常全文。
- JSON 记录是普通任务约定，不是 G/R/P 实验验收报告。

## 2. 查询可用项目

`GET /api/projects`

用途：让任务页展示本机已配置的真实项目。服务端配置将 `projectId` 映射到仓库目录，前端不提交任意目录。M1 仅支持本地 Git 仓库和修复类型任务。

成功：`200 OK`。

```json
{
  "projects": [
    {"projectId": "sample-python", "name": "示例 Python 项目"}
  ]
}
```

以上为虚构示例。未配置项目时返回空数组，前端提示先配置项目并禁用提交；配置文件损坏或不可读返回 `503 PROJECT_CONFIG_UNAVAILABLE`，不能伪装为空项目列表。项目列表不代表测试依赖已准备好。

本机配置为 `{"projects":[{"projectId":"sample-python","name":"示例 Python 项目","repo":"仓库根目录"}]}`，默认 `runs/tasks/projects.json`，缺失视为未配置。`repo` 相对配置文件目录解析，须指向 Git 工作树根目录；不会返回网页。配置读取在每次请求时进行，修改后不要求重启。

## 3. 创建任务

`POST /api/tasks`

请求头：`Content-Type: application/json`。

```json
{
  "projectId": "sample-python",
  "revision": "main",
  "kind": "fix_bug",
  "current": "add(2, 3) 返回 -1",
  "expected": "应返回 5",
  "allowedPaths": ["calc.py"],
  "allowTests": true,
  "checkTargets": ["test_calc.py"],
  "maxSteps": 12
}
```

### 3.1 字段规则

| 字段 | 必填 / 默认 | 规则 |
| --- | --- | --- |
| `projectId` | 必填 | 字符串，1–64 字符，仅小写字母、数字、`-`、`_`；须存在于本机配置 |
| `revision` | 必填 | 字符串，去首尾空白后 1–160 字符，不以 `-` 开头，不含控制字符；须能在配置仓库解析为 commit |
| `kind` | 默认 `fix_bug` | M1 仅接受 `fix_bug`；阅读与功能类型以后扩展 |
| `current` | 必填 | 字符串，去首尾空白后 1–4000 字符 |
| `expected` | 必填 | 字符串，去首尾空白后 1–4000 字符 |
| `allowedPaths` | 必填 | 非空字符串数组，最多 100 项，每项最多 500 字符；规则见下文 |
| `allowTests` | 必填 | 严格布尔类型 |
| `checkTargets` | 必填 | 字符串数组，最多 20 项，每项最多 220 字符；允许测试时非空，否则必须为 `[]` |
| `maxSteps` | 默认 `12` | 整数 1–50，不接受布尔值 |

允许修改路径使用仓库相对路径和 `/` 分隔符，支持文件或目录，不支持通配符。拒绝绝对路径、盘符、反斜杠、以 `-` 开头、空段、`.`/`..` 路径段、控制字符和无效 Unicode；拒绝 `:*?"<>|` 以及以空格或点结尾的路径段。M1 保护段为 `.git`、`.venv`、`venv`、`node_modules` 及以 `.env` 开头的名称，不区分大小写；后续执行约束只能更严格，不能因已保存就放行受保护路径。

检查目标仅接受仓库相对 pytest 路径，或 `文件.py::测试节点`，不接受命令文本、选项或绝对路径。文件部分遵循相对路径规则，节点部分非空且不得包含控制字符。后续执行必须使用参数数组和已配置解释器，不把字段拼成 shell 命令。

数组元素去首尾空白，空项拒绝，重复项拒绝。正文内部空白保持；默认值和去空白后的值反映在返回的约定中。M1 只校验和保存约定，不证明路径的实际可写性、测试存在或执行安全；这些还需后续在固定版本副本内检查和约束。

### 3.2 后端处理顺序

1. 校验 HTTP/JSON 与字段，不生成任务记录来掩盖无效输入。
2. 查本机项目配置，将 `revision` 解析为准确 `baseCommit`，不执行仓库代码或模型。
3. 生成完整随机编号，独占创建任务目录，写入权威记录。
4. 正式文件保存成功后返回 `201 Created`，并带 `Location: /api/tasks/<taskId>`。

当前源仓库的未提交内容不进入本轮；只保存所选 commit。约定确认页应展示准确 commit 和这一含义。创建与查询阶段不创建 worktree，也不安装依赖。

### 3.3 成功响应

```json
{
  "schemaVersion": 1,
  "taskId": "6f8d2c91a7b04e238aa13f2c704d9b65",
  "recordType": "actual",
  "status": "created",
  "createdAt": "2026-10-31T02:00:00Z",
  "contract": {
    "projectId": "sample-python",
    "requestedRevision": "main",
    "baseCommit": "1111111111111111111111111111111111111111",
    "kind": "fix_bug",
    "current": "add(2, 3) 返回 -1",
    "expected": "应返回 5",
    "allowedPaths": ["calc.py"],
    "allowTests": true,
    "checkTargets": ["test_calc.py"],
    "maxSteps": 12
  }
}
```

示例 commit 为占位值，实际必须来自 Git 解析结果。`recordType: actual` 表示真实保存的普通任务，不表示已经执行或检查通过。`status: created` 表示尚未启动。文件记录与成功返回采用相同结构；`taskId` 必须匹配所在目录编号。

M1 不支持修改、删除或启动任务。保存后需求变更须新建任务。一次 POST 对应一次创建，M1 不提供幂等键：网页保存期间禁用重复提交，不自动重试。网络断开导致未收到响应时，提示“保存结果暂无法确认，勿直接重复提交”；后端可能已保存成功。历史查询/幂等能力在后续批次补充，不声称现在已解决该情况。

## 4. 按编号查询任务

`GET /api/tasks/{taskId}`

成功：`200 OK`，返回与创建成功相同的记录结构。本轮只有 `created` 状态；前端显示“已保存，尚未启动”，执行按钮继续禁用。

查询先校验编号格式，再访问固定根目录下的对应任务；不得把 URL 参数直接当任意文件路径。读取时检查文件结构、版本、编号与目录一致性，拒绝越界或符号链接指向的记录。

编号格式错误返回 `400 INVALID_TASK_ID`；格式有效但不存在返回 `404 TASK_NOT_FOUND`；正式文件存在但 JSON/结构损坏返回 `500 TASK_RECORD_CORRUPT`；IO 故障返回 `503 STORAGE_UNAVAILABLE`。记录损坏不能当作任务不存在，也不能静默生成新任务。

网页详情地址建议 `/#/tasks/{taskId}`，hash 路由由前端处理，加载时再调用上述 GET。刷新时不得再次发送创建请求。临时文件不能作为有效查询结果；只有临时文件时返回任务不存在，并在本机诊断中保留未完成写入信息。

## 5. 统一错误结构

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "任务信息不符合要求",
    "fields": [
      {"field": "checkTargets", "message": "允许运行测试时必须指定检查目标"}
    ]
  }
}
```

`fields` 始终为数组，无字段错误时为 `[]`。`code` 用于前端判断，`message` 用于人读；不原样回显敏感输入或底层异常。

| HTTP 状态 | 错误码 | 条件 |
| --- | --- | --- |
| 400 | `INVALID_JSON` / `VALIDATION_ERROR` | 无效 JSON、重复键、字段不符合规则 |
| 400 | `INVALID_TASK_ID` | 编号格式不合法 |
| 400 | `INVALID_REVISION` | 版本无法解析为 commit |
| 400 | `UNKNOWN_PROJECT` | 指定项目不在配置中 |
| 403 | `ORIGIN_REJECTED` | 跨站或不匹配的来源写请求 |
| 404 | `TASK_NOT_FOUND` / `ROUTE_NOT_FOUND` | 任务或路由不存在 |
| 405 | `METHOD_NOT_ALLOWED` | 已定义路径使用不支持的方法；带 Allow 头 |
| 413 | `REQUEST_TOO_LARGE` | 请求体超过 64 KiB |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | POST 未使用 JSON 内容类型 |
| 500 | `TASK_RECORD_CORRUPT` / `INTERNAL_ERROR` | 记录损坏或未预期程序错误 |
| 503 | `STORAGE_UNAVAILABLE` / `PROJECT_CONFIG_UNAVAILABLE` / `PROJECT_UNAVAILABLE` | IO、配置或配置仓库/Git 不可用 |

错误响应不算已创建。若服务器在正式记录落盘后发送响应失败，客户端结果可能未知，按前述网络失败规则处理，不自动删除已存任务。

## 6. 前后端联调场景

| 场景 | 接口预期 | 页面预期 |
| --- | --- | --- |
| 有效任务 | POST 201，GET 200，约定一致 | 跳转含编号的详情页，显示准确 commit 和未启动 |
| 暂不检查 | `allowTests=false` 且目标为空可保存 | 展示“暂不检查” |
| 无可用项目 | 项目列表空 | 提示配置项目，不拿演示项目提交 |
| 字段或版本错误 | 400 与具体错误 | 保留表单，说明原因，不跳转成功页 |
| 刷新/重启服务 | GET 恢复原记录 | 无需重新提交 |
| 任务不存在 | 404 | 展示不存在，可返回新建页 |
| 存储损坏或故障 | 500/503 | 展示读取/保存失败，不冒充空记录 |
| POST 响应丢失 | 客户端不能断言未保存 | 提示结果未知，不自动重发 |
| 演示页面 | 不调用创建真实任务接口 | 明确演示标识，不能混用真实编号 |

场景对应的实际检查范围见[验收记录](m1-verification.md)，不能把测试计划当作所有浏览器场景均已验证。后续启动、事件、结果、材料下载和历史列表接口不在本轮契约内。
