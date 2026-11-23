# P0 定向验收

2026-11-17 按用户要求运行离线及真实容器检查，不调用模型。

离线使用现有 coding-agent Conda 执行 test_p0_acceptance、test_evaluation_checks、test_check_quality、test_snapshots、test_file_io，结果 **49 passed、23 subtests passed**。新增检查覆盖 Django 目标映射与结果解析、大文件 CRLF 修改、过期快照、写临时文件期间发生外部修改、模拟 Windows 替换被拒时保留原文件并清理临时文件，以及即时语法提示。

真实 Django 容器检查使用固定 G 的隔离工作副本，原生入口执行 `tests/forms_tests/tests/test_media.py`，结果 17 项通过，导入路径为 `/testbed/django`，未出现 pytest 缺失。随后在同一隔离副本注入语法错误，重新运行开发检查，testbed 解释器返回 syntax_error，准确报告 `django/forms/widgets.py` 第 1085 行。验收目录为本机 `runs/evaluation/swe-bench/run_20261117-154314_6537b0d2/`，含两次结果和容器日志，模型调用 0。

最终 Docker 没有运行中的容器，本轮容器已清理。Docker 引擎可见内存约 7.76 GiB，WSL 8 GB 上限仍有效。

上述并发检查覆盖临时文件写入期间发生的修改。当前大文件实现最后一次哈希核对和 `os.replace` 之间仍存在外部写入竞争窗口，模拟 Windows 替换失败也不等于覆盖所有 Windows 共享模式。不能宣称并发覆盖保护已经完全成立，须先解决或明确约束该窗口再安排真实模型复测。

后续用户明确决定沿用当前快照哈希检查和临时文件替换，接受最后核对到替换之间的外部修改窗口。该项作为已知限制记录，不再设置额外并发保护作为复测前置条件。
