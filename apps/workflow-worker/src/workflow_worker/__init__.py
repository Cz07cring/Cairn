"""Python Temporal Worker 部署入口（apps/workflow-worker）。

与 Broker 不同：本进程**可以**为 Kernel Activities 持有 RING_DATABASE_URL，
经 control_kernel 访问业务库。Workflow 代码仍禁止直接 SQL/HTTP/文件 IO。
不得将 Goal 标为 DONE；不得在 Temporal 不可用时 fallback LEGACY。
"""

from .service import health, run_loop

__all__ = ["health", "run_loop"]
