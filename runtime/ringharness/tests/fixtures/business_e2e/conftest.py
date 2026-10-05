"""本目录是「被测样例工程」，**不属于本仓测试套件**。

为什么需要本文件：`order_service/` 是一个自包含的样例仓库模板，供业务 E2E
检出到 `.runtime/e2e/<run_id>/` 后、**以该工程自身为工作目录**运行其测试
（见 `doc/research/真实业务E2E驱动开发案例-订单幂等修复-2026-09-12.md` §3）。

根配置为 `testpaths = ["tests"]`，故 `pytest -q` 会递归进入本目录，把样例工程的
`tests/` 与 `tests_hidden/` 当作本仓测试收集 —— 而它们 `import order_service`
依赖的是**该工程本地包**，在仓库根下不在 `sys.path`，于是产生：

    ModuleNotFoundError: No module named 'order_service'

（CI 上表现为 `uv run pytest -q` 直接失败并中断，ruff/build/契约后续步骤全被 skip。）

`collect_ignore` 让 pytest 跳过该子目录：样例工程的测试仍在仓内可复现，
但**只在其自身目录下**由 E2E 流程驱动运行，不污染本仓门禁。
"""

# 相对本文件所在目录
collect_ignore = ["order_service"]
