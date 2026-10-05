"""workflow-worker 长驻：缺 RING_TEMPORAL_TARGET 时诚实空闲。

边界：
- 与 Broker 不同：允许 RING_DATABASE_URL，供 Kernel Activities 经 control_kernel 读/写业务库。
- Workflow 本身不得直接 SQL/HTTP/文件；IO 仅在 Activity。
- 不得将 Goal 标为 DONE；不得 fallback LEGACY。
- 未设置 RING_TEMPORAL_TARGET：``--once`` 打 ``workflow-worker-idle`` 并以 0 退出。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from orchestration.client import DEFAULT_TASK_QUEUE

logger = logging.getLogger("workflow_worker")

# Worker 构建身份；与 deploy/temporal/VERSIONS.md 对齐
WORKER_BUILD_ID = "m0-py-1.32.0-dev"


@dataclass(frozen=True)
class WorkerSettings:
    """运行时配置；temporal_target 缺省表示无法连接，只能空闲。"""

    temporal_target: str | None
    namespace: str = "default"
    task_queue: str = DEFAULT_TASK_QUEUE
    database_url: str | None = None


def load_settings(environ: Mapping[str, str] | None = None) -> WorkerSettings:
    env = os.environ if environ is None else environ
    target = (env.get("RING_TEMPORAL_TARGET") or "").strip() or None
    namespace = (env.get("RING_TEMPORAL_NAMESPACE") or "default").strip() or "default"
    task_queue = (env.get("RING_TEMPORAL_TASK_QUEUE") or DEFAULT_TASK_QUEUE).strip() or DEFAULT_TASK_QUEUE
    # 与 Broker 相反：Worker 可为 Kernel Activities 持有业务库 URL（经 control_kernel）
    database_url = (env.get("RING_DATABASE_URL") or "").strip() or None
    return WorkerSettings(
        temporal_target=target,
        namespace=namespace,
        task_queue=task_queue,
        database_url=database_url,
    )


def refuse_mark_goal_done(*_args, **_kwargs) -> None:
    """显式拒绝：Worker / Workflow 完成都不能写成 Goal DONE。"""
    raise PermissionError(
        "workflow-worker 不得将 Goal 标为 DONE；完成判定仅属 ControlKernel + VerificationProfile"
    )


def health() -> dict:
    """进程健康视图；声明不写 Goal DONE、非 LEGACY fallback。"""
    return {
        "status": "ok",
        "role": "workflow-worker",
        "worker_build_id": WORKER_BUILD_ID,
        "task_queue": DEFAULT_TASK_QUEUE,
        "marks_goal_done": False,
        "legacy_fallback": False,
        "may_hold_business_db": True,
    }


async def _run_worker(settings: WorkerSettings) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from orchestration.temporal_workflows import GoalWorkflow
    from temporalio.client import Client
    from temporalio.worker import Worker

    from .activities import KERNEL_ACTIVITIES

    assert settings.temporal_target is not None
    if settings.database_url:
        logger.info(
            "workflow-worker-db-configured note=Kernel Activities 可经 control_kernel 使用业务库"
        )
    client = await Client.connect(settings.temporal_target, namespace=settings.namespace)
    # 同步 Kernel Activities 需线程池；禁止在 Workflow 事件循环上直接跑 SQL
    with ThreadPoolExecutor(max_workers=8) as activity_executor:
        worker = Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[GoalWorkflow],
            activities=list(KERNEL_ACTIVITIES),
            activity_executor=activity_executor,
            build_id=WORKER_BUILD_ID,
        )
        logger.info(
            "workflow-worker-running target=%s queue=%s build_id=%s",
            settings.temporal_target,
            settings.task_queue,
            WORKER_BUILD_ID,
        )
        await worker.run()


async def _connect_once(settings: WorkerSettings) -> int:
    """``--once`` 且已配置 target：验证可连并注册 Worker，不长驻。"""
    from concurrent.futures import ThreadPoolExecutor

    from orchestration.temporal_workflows import GoalWorkflow
    from temporalio.client import Client
    from temporalio.worker import Worker

    from .activities import KERNEL_ACTIVITIES

    assert settings.temporal_target is not None
    client = await Client.connect(settings.temporal_target, namespace=settings.namespace)
    # 构造 Worker 以确认注册面；不调用 run()，避免单测挂起
    with ThreadPoolExecutor(max_workers=2) as activity_executor:
        Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[GoalWorkflow],
            activities=list(KERNEL_ACTIVITIES),
            activity_executor=activity_executor,
            build_id=WORKER_BUILD_ID,
        )
    logger.info(
        "workflow-worker-once connected target=%s queue=%s build_id=%s",
        settings.temporal_target,
        settings.task_queue,
        WORKER_BUILD_ID,
    )
    return 0


def run_loop(
    *,
    once: bool = False,
    settings: WorkerSettings | None = None,
) -> int:
    """主循环。缺 target 时诚实空闲；有 target 则连接并跑 Worker。"""
    cfg = load_settings() if settings is None else settings
    if cfg.temporal_target is None:
        logger.info("workflow-worker-idle")
        if once:
            return 0
        # 无 target 时长驻也会空转退出：避免假称已接入 Temporal
        logger.info("workflow-worker-idle exiting: RING_TEMPORAL_TARGET 未设置")
        return 0

    if once:
        return asyncio.run(_connect_once(cfg))
    asyncio.run(_run_worker(cfg))
    return 0
