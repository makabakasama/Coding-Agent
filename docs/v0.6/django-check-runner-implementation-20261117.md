# Django 开发检查入口实施

按 P0-A 方案接入 Django 仓库原生开发测试入口。`run_tests` 的 Django 测试文件目标映射成 `tests/runtests.py` 测试标签，例如 `tests/forms_tests/tests/test_media.py` 对应 `forms_tests.tests.test_media`。使用 testbed 解释器、test_sqlite 设置和单进程。保留目标 `::` 测试选择器，无法映射的路径明确报错。

容器记录实际命令，并在执行 Django 仓库测试前确认原生入口及 django 模块可导入。解析 unittest 风格的测试计数、失败、错误及跳过；没有识别到实际通过测试不能记为通过。临时脚本、SymPy 及其他仓库仍沿用原入口。检查进程异常时返回脱敏后的 stderr 摘要，以便模型看到环境或导入问题。

本批为代码实施和静态核对，尚未运行新增回归、容器、真实模型或官方评分。具体 Django 版本的目标映射、依赖和检查分类仍需定向验收。P0-A 其他仓库入口预检、P0-B 大文件读取及编辑语法反馈尚未实施。

后续补充了其他仓库入口预检：SymPy 确认 bin/test 存在，默认 pytest 路径确认 testbed 解释器可找到 pytest。失败时记录启动命令的 stderr 或 stdout，并将脱敏摘要返回模型。上述预检在每次开发检查容器中执行，尚未实现任务准备阶段的预检；新增行为尚未运行测试或容器验收。
