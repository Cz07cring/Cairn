"""失租可见性：观察期发现活动处于 RECOVERING（租约已丢）时，必须写进工作流返回值。

**为什么需要**：租约过期时 `expire_stale_leases` 把活动置 `RECOVERING` —— 这是一个
**非终态**。若不显式记录，编排层只能看到「非终态」，**无法把「租约已丢、工作已废」
与「正常推进」区分开**，于是一路观察到 `OBSERVE_TIMEOUT`（默认 ≈24min）才开始恢复；
而实际工作早已作废（effect 全被 409 拒、资源/预算已 QUARANTINED）。

实测代价（2026-09-12）：官方多步循环跑了 **4 轮、发出 3 个工具调用，却 0 个生效**
（全是 `INVALID_STATE: attempt 已非 ACTIVE`），从外部日志看却像「模型没产出工具调用」，
排查绕远。本用例钉住「该信号必须可见」，避免回归。

**本文件只覆盖「记录」，不覆盖「提前退出」** —— 改控制流会动命令序列、须另开 patch 门，
属产品/内核决策，此处刻意不动。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from orchestration.temporal_workflows import (
    _ACTIVITY_LEASE_LOST_STATUS,
    GoalWorkflow,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"
_FIXED_ACTIVITY_ID = "act-lease-lost-1"


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-lease-lost",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="record_recovery_abandonment")
async def _stub_abandon(
    goal_id: str, reason: str, generation: int, prior_run_id: str | None
) -> dict:
    return {"abandonment_id": "a1", "goal_id": goal_id, "reason": reason,
            "generation": generation, "prior_run_id": prior_run_id,
            "marks_goal_done": False}


@activity.defn(name="list_runtime_actions")
async def _stub_list(goal_id: str, owner_epoch: str) -> dict:
    return {
        "actions": [
            {
                "activity_id": _FIXED_ACTIVITY_ID,
                "action_id": _FIXED_ACTIVITY_ID,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="admit_plan_action")
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    return {"activity_id": activity_id, "attempt_id": "att-1", "fencing_epoch": "1"}


@activity.defn(name="RunActivation")
async def _stub_run(payload: dict) -> dict:
    return {"status": "PENDING_ENV", "reason": "stub", "kind": "PLAN", "pending_harness": True}


@activity.defn(name="observe_activity_status")
async def _stub_observe_lease_lost(activity_id: str) -> dict[str, str]:
    """模拟租约过期后的活动状态：RECOVERING（非终态）。"""
    return {
        "activity_id": activity_id,
        "status": _ACTIVITY_LEASE_LOST_STATUS,
        "kind": "PLAN",
    }


async def _run_workflow(payload: dict) -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        assert env.client is not None
        async with (
            Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[
                    _stub_ensure,
                    _stub_abandon,
                    _stub_list,
                    _stub_admit,
                    _stub_observe_lease_lost,
                ],
            ),
            Worker(env.client, task_queue=_RUNNER_QUEUE, activities=[_stub_run]),
        ):
            # observe_max_ticks=1 且不启用 CAN ⇒ 一轮观察后 OBSERVE_TIMEOUT 即返回，
            # 工作流**会终止**（这正是本用例能安全 await 的原因）。
            return await asyncio.wait_for(
                env.client.execute_workflow(
                    GoalWorkflow.run,
                    payload,
                    id=f"goal-lease-lost-{uuid4()}",
                    task_queue=_CONTROL_QUEUE,
                ),
                timeout=60,
            )


def _payload(**extra: object) -> dict:
    base = {
        "command_id": f"cmd-lease-{uuid4()}",
        "goal_id": str(uuid4()),
        "owner_epoch": "1",
        "generation": 0,
        "observe_max_ticks": 1,
    }
    base.update(extra)
    return base


def test_lease_lost_status_constant_matches_kernel_marker() -> None:
    """常量须与 Kernel 在失租时写入的状态一致（改这个名字要及时同步）。"""
    assert _ACTIVITY_LEASE_LOST_STATUS == "RECOVERING"


def test_lease_lost_is_surfaced_in_workflow_result() -> None:
    """**核心**：观察到 RECOVERING 时，工作流返回值必须列出该活动。"""
    result = asyncio.run(_run_workflow(_payload()))
    assert result["lease_lost_activity_ids"] == [_FIXED_ACTIVITY_ID]
    # 仍然不得写业务终态
    assert result.get("marks_goal_done") is False


def test_no_lease_loss_leaves_list_empty() -> None:
    """未发生失租时不产生噪声（列表为空）。"""

    @activity.defn(name="observe_activity_status")
    async def _stub_observe_running(activity_id: str) -> dict[str, str]:
        return {"activity_id": activity_id, "status": "RUNNING", "kind": "PLAN"}

    async def _run() -> dict:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_abandon,
                        _stub_list,
                        _stub_admit,
                        _stub_observe_running,
                    ],
                ),
                Worker(env.client, task_queue=_RUNNER_QUEUE, activities=[_stub_run]),
            ):
                return await asyncio.wait_for(
                    env.client.execute_workflow(
                        GoalWorkflow.run,
                        _payload(),
                        id=f"goal-no-loss-{uuid4()}",
                        task_queue=_CONTROL_QUEUE,
                    ),
                    timeout=60,
                )

    result = asyncio.run(_run())
    assert result["lease_lost_activity_ids"] == []
