# M1 本地运行与检查

这是 M1 历史批次说明。当前包含 Agent、三类任务及实验历史的操作请使用 [工作台使用指南](workbench-guide.md)，版本进度见 [阶段报告](version-report.md)。下述端口和限制仅描述 M1 当时状态。

本轮只创建、保存和查询普通任务。没有 Agent 启动、目标测试或独立副本创建功能。

## 1. 启动工作台

在项目根目录运行（Python 3.11+、Git）：

```powershell
python -m repo_agent.task_http --port 4173
```

打开 [本地工作台](http://127.0.0.1:4173)。停止服务按 Ctrl+C。原静态服务若占用此端口须先停止；不能同时使用普通静态服务作为 API。

本轮保留了已占用 4173 的旧服务，已启动工作台位于 [http://127.0.0.1:4175](http://127.0.0.1:4175)。以后可使用 `--port 4175` 重启；独占绑定会明确拒绝已占用端口。

本机本轮已在缺少配置时创建 `runs/tasks/projects.json`，登记当前 Coding-Agent checkout，未覆盖既有配置。这是忽略目录中的本机设置，新克隆不会自带。

## 2. 项目配置

默认配置路径是 `runs/tasks/projects.json`。不存在时项目列表为空，页面禁用保存。结构如下：

```json
{
  "projects": [
    {
      "projectId": "coding-agent",
      "name": "Coding-Agent",
      "repo": "../.."
    }
  ]
}
```

`repo` 相对配置文件所在目录解析；示例中 `runs/tasks/../..` 就是当前项目根目录，也可自行在本机配置其他 Git 仓库根目录。配置可含本机路径，但不应复制到公开材料。不需要填写 API 密钥或准备目标测试依赖。

项目接口只返回编号和展示名称。配置缺失视为未配置；损坏、不可读视为配置错误。项目目录必须是 Git 工作树根目录，不能静默采用某个父仓库。

使用隔离设置：

```powershell
python -m repo_agent.task_http --port 4174 --data-dir runs/tasks-local --projects runs/tasks/projects.json
```

服务只监听回环地址，按白名单提供页面资源；不能从该端口浏览仓库目录、`.env` 或 `runs/` 文件。

## 3. 手工检查一轮保存

1. 选择项目，版本填写 `HEAD`；填写当前行为和期望行为。
2. 填写仓库相对修改路径；检查目标每行一个。若暂不检查，取消测试权限。
3. 预览后确认保存；成功进入带完整随机编号的详情地址。
4. 详情应显示准确 commit、创建时间、原约定和“已保存，尚未启动”。收藏此地址。
5. 刷新，仍显示同一份任务；停止服务、用相同配置和数据目录重启，再打开收藏地址。

任务记录保存到 `runs/tasks/task_<ID>/task.json`。保存不会创建 worktree、修改项目业务文件或调用模型；输入的路径和测试目标仅做约定校验，尚未验证目标存在或执行效果。

版本填不存在的引用，应显示 `INVALID_REVISION` 并保留表单；不要用真实任务反复制造存储故障。记录损坏、IO 失败、跨站请求等由合成测试覆盖。

如果保存时断网或响应丢失，页面可能不知道服务端是否已保存；不要直接重发。M1 没有幂等键、真实历史列表或恢复未知提交的自动机制，需核对本机记录；刷新不代表之前未保存。

## 4. 开发检查

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_tasks.py -v
node --check frontend/app.js
node --test frontend/tests/task-ui.test.cjs
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

前三项分别是新后端专项、前端语法和前端模拟行为检查；第四项是全项目离线回归。现有全量用例依赖 pytest，使用项目已准备的 `.venv`；新克隆按根 README 准备环境。前端 Node 测试不代替浏览器检查。全部都不要求真实模型凭据。

本轮验收与限制见 [M1 验收记录](m1-verification.md)；接口字段与错误码见 [接口契约](task-api.md)。
