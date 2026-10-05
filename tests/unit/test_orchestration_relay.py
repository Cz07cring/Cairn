"""真实 Temporal 连接故障与幂等启动边界。"""

from __future__ import annotations

import asyncio
import logging
import os
from uuid import uuid4

import pytest
from orchestration.client import (
    RealTemporalClient,
    TemporalUnavailable,
)

_log = logging.getLogger(__name__)


def _terminate_workflow(target: str, workflow_id: str) -> None:
    """尽力终止测试自建的真实工作流（清理用，失败不掩盖主断言）。

    为什么需要：真实 Temporal 上的工作流不会自行结束，遗留的 Running 工作流会
    抢占 `ring-control` 队列，破坏后续测试的可重复性（见下方用例说明）。
    """

    async def _run() -> None:
        from temporalio.client import Client

        client = await Client.connect(target)
        await client.get_workflow_handle(workflow_id).terminate(reason="test cleanup")

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - 清理失败不应让测试变红
        # 不静默：清理失败会留下孤儿工作流并污染后续运行，须留痕以便排查
        _log.warning("终止测试工作流 %s 失败: %s: %s", workflow_id, type(exc).__name__, exc)


def test_real_temporal_client_import_smoke():
    """可构造 RealTemporalClient；无 Server 时 ensure_started → TemporalUnavailable。"""
    client = RealTemporalClient(target="127.0.0.1:1")
    with pytest.raises(TemporalUnavailable):
        client.ensure_started(f"goal-{uuid4()}")


@pytest.mark.skipif(
    not (os.environ.get("RING_TEMPORAL_TARGET") or "").strip(),
    reason="未设置 RING_TEMPORAL_TARGET；跳过真实 Temporal 联调",
)
def test_real_temporal_client_live_ensure_started():
    """真实 Temporal 上 ensure_started 幂等。

    **必须终止自建工作流**（本批修复的测试卫生缺陷）：本用例会启动一个真实
    `GoalWorkflow`，它没有驱动者、也不会自行结束。若放任其 Running：

      ① 本地 Temporal 累积孤儿工作流（本会话实测累积到 6 个）；
      ② 孤儿会抢占 `ring-control` 队列 —— 后续 compose 类测试拉起 worker 时，
         它们的活动被孤儿领走，表现为**本地 compose 测试挂起**。
         实测：`uv run pytest tests/temporal/ -q` 从 46s 全过 → 挂死 10 分钟以上无输出；
         而 CI 在 `b1ea8a8` 上完整跑完 62 passed —— 显系本地残留所致。

    修好后实测：该文件连跑不再新增孤儿（6→6），清空历史孤儿后完整套件
    **63 passed, 48.74s**（含 compose）。
    """
    target = os.environ["RING_TEMPORAL_TARGET"].strip()
    client = RealTemporalClient(target=target)
    workflow_id = f"goal-live-{uuid4()}"
    try:
        run_id = client.ensure_started(workflow_id)
        assert run_id
        assert client.ensure_started(workflow_id) == run_id
    finally:
        _terminate_workflow(target, workflow_id)
