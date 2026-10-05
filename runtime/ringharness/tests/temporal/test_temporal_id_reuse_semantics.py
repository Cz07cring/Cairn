"""钉住 Temporal 的 id 复用语义 —— 它决定了 ensure_started 哪些分支真实可达。

**为什么需要**：本会话修 `ensure_started` 时我先按「已关闭 → 抛 AlreadyStarted → 查状态」
推理了一遍，**实测把它推翻了**（见下）。库/服务端策略一旦变化，编排侧的分支可达性
随之变化，而**推断看不出来**。故把实测语义钉成测试：策略变了，这里会红。

实测（本机 Temporal，2026-09-12）：

    Client.start_workflow 默认 id_reuse_policy = ALLOW_DUPLICATE (=1)
    id_conflict_policy 默认 = UNSPECIFIED (=0)

    | 既有 run 状态 | 再以同 id start 的行为            | 编排侧后果                       |
    |---------------|-----------------------------------|----------------------------------|
    | RUNNING       | 抛 WorkflowAlreadyStartedError    | 走 describe→分流（复用 run_id）  |
    | COMPLETED     | **不抛错，静默开新 run**          | **不经**分流 ⇒ 隐式重新驱动      |

⇒ 后者是**隐式重新驱动**：命令确实会被处理，但「已完成的 Goal 被重新驱动」
既无日志也无闸门。是否允许属领域裁决（可能与恢复流程耦合），
本批只**记录事实**，不改策略 —— 若哪天改成 REJECT_DUPLICATE，
下面 `test_completed_id_is_silently_reused` 会红，正好提醒来更新编排侧的假设。

**自清理**：凡启动真实 workflow 的用例必须在 finally 终止它 ——
遗留的 Running workflow 会抢占任务队列，导致本地套件挂死（见 c29 的教训）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

_log = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("RING_TEMPORAL_TARGET") or "").strip(),
    reason="未设置 RING_TEMPORAL_TARGET；跳过真实 Temporal 语义实测",
)


@workflow.defn(name="ProbeReturnsImmediately")
class ProbeReturnsImmediately:
    @workflow.run
    async def run(self, value: int) -> int:
        return value + 1


@workflow.defn(name="ProbeSleeps")
class ProbeSleeps:
    @workflow.run
    async def run(self, seconds: int) -> str:
        await workflow.sleep(seconds)
        return "done"


def _status_of(desc: object) -> str:
    """取状态名；describe 的 status 类型上可为 None，统一归一为字符串。"""
    status = getattr(desc, "status", None)
    name = getattr(status, "name", None)
    return str(name) if name else str(status)


async def _terminate(client: Client, workflow_id: str) -> None:
    """尽力终止（清理用；失败留痕，不掩盖主断言）。"""
    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.terminate(reason="test cleanup")
    except Exception as exc:  # noqa: BLE001 - 可能已关闭，属预期
        _log.warning("终止测试 workflow %s 未成功（可能已关闭）: %s", workflow_id, exc)


def test_completed_id_is_silently_reused() -> None:
    """**已 COMPLETED 的 id 会被静默重开**（不抛错）—— 这正是编排侧看不到的隐式重驱动。

    若本用例变红（例如抛了 WorkflowAlreadyStartedError），说明默认复用策略被改成
    REJECT_DUPLICATE/_FAILED_ONLY —— 那时 `_require_open_run` 的「已关闭」分支
    会突然变为可达，必须回去确认编排侧对 TemporalWorkflowClosed 的处理。
    """
    target = os.environ["RING_TEMPORAL_TARGET"].strip()
    workflow_id = f"probe-completed-{uuid4()}"
    queue = f"probe-completed-q-{uuid4().hex[:8]}"

    async def _run() -> None:
        client = await Client.connect(target)
        try:
            async with Worker(
                client, task_queue=queue, workflows=[ProbeReturnsImmediately]
            ):
                result = await client.execute_workflow(
                    ProbeReturnsImmediately.run, 41, id=workflow_id, task_queue=queue
                )
            assert result == 42
            desc = await client.get_workflow_handle(workflow_id).describe()
            assert _status_of(desc) == "COMPLETED"

            # 关键断言：不抛错，且开出**不同**的 run_id
            second = await client.start_workflow(
                ProbeReturnsImmediately.run, 1, id=workflow_id, task_queue=queue
            )
            assert str(second.result_run_id) != str(desc.run_id), "应开出一个新的 run"
        finally:
            await _terminate(client, workflow_id)

    asyncio.run(_run())


def test_running_id_raises_already_started() -> None:
    """**RUNNING 时再 start 会抛错** —— 这解释了我们 except 分支为何可达。"""
    target = os.environ["RING_TEMPORAL_TARGET"].strip()
    workflow_id = f"probe-running-{uuid4()}"
    queue = f"probe-running-q-{uuid4().hex[:8]}"

    async def _run() -> None:
        client = await Client.connect(target)
        try:
            async with Worker(client, task_queue=queue, workflows=[ProbeSleeps]):
                first = await client.start_workflow(
                    ProbeSleeps.run, 30, id=workflow_id, task_queue=queue
                )
                desc = await client.get_workflow_handle(workflow_id).describe()
                assert _status_of(desc) == "RUNNING"

                with pytest.raises(WorkflowAlreadyStartedError) as exc:
                    await client.start_workflow(
                        ProbeSleeps.run, 30, id=workflow_id, task_queue=queue
                    )
                assert str(exc.value.run_id) == str(first.result_run_id)
        finally:
            await _terminate(client, workflow_id)

    asyncio.run(_run())
