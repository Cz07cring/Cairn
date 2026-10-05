"""激活预算必须**真的**进入派发命令 —— 用工作流历史验证，而非只测纯函数。

纯函数单测证明「算得对」；本文件证明「真的被用上」，即 `payload.activation_budget_seconds`
确实改变了 `ScheduleActivityTask` 命令的 `start_to_close_timeout`。
两者缺一不可 —— 本仓已多次出现「实现正确但没接上」。

做法：不等待工作流自然结束（`GoalWorkflow` 派发后会继续轮询后续动作，不会立即返回），
而是**读工作流历史**取出派发命令的属性，然后取消。这样既验证了真实命令，
又避免「取消与 worker 关闭互相等待」导致的挂起（本批实测过该挂死）。
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

from orchestration.temporal_workflows import (
    _ACTIVATION_BUDGET_PATCH,
    _DEFAULT_ACTIVATION_BUDGET_SECONDS,
    GoalWorkflow,
)
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"
_FIXED_ACTIVITY_ID = "act-budget-hist-1"


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-budget-hist",
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
    # 永不返回：让工作流停在「已派发、正在观察」状态，便于读历史
    await asyncio.sleep(3600)
    return {}


@activity.defn(name="observe_activity_status")
async def _stub_observe(activity_id: str) -> dict[str, str]:
    return {"activity_id": activity_id, "status": "RUNNING", "kind": "PLAN"}


def _payload(**extra: object) -> dict:
    base = {
        "command_id": f"cmd-hist-{uuid4()}",
        "goal_id": str(uuid4()),
        "owner_epoch": "1",
        "generation": 0,
    }
    base.update(extra)
    return base


def _run_activation_timeout(history) -> float | None:
    """从历史里取 RunActivation 那次 ScheduleActivityTask 的 start_to_close_timeout（秒）。

    注意：protobuf 的 `Duration` **不等于** `datetime.timedelta`（比较会失败，
    尽管打印出来都是 `seconds: 360`）。故统一换算为秒数再比。
    """
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            continue
        attrs = event.activity_task_scheduled_event_attributes
        if attrs.activity_type.name == "RunActivation":
            return attrs.start_to_close_timeout.ToTimedelta().total_seconds()


async def _scheduled_timeout(payload: dict) -> float | None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        assert env.client is not None
        async with (
            Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[_stub_ensure, _stub_abandon, _stub_list, _stub_admit, _stub_observe],
            ),
            Worker(env.client, task_queue=_RUNNER_QUEUE, activities=[_stub_run]),
        ):
            handle = await env.client.start_workflow(
                GoalWorkflow.run,
                payload,
                id=f"goal-hist-{uuid4()}",
                task_queue=_CONTROL_QUEUE,
            )
            # 等派发命令进入历史（最多 20s；本地测试服务器通常亚秒级）
            history = None
            for _ in range(400):
                history = await handle.fetch_history()
                if _run_activation_timeout(history) is not None:
                    break
                await asyncio.sleep(0.05)
            timeout = _run_activation_timeout(history) if history else None
            # 取消但**不无限等待**（避免与 worker 关闭互相等待而挂起）
            with contextlib.suppress(Exception):
                await asyncio.wait_for(handle.cancel(), timeout=5)
            return timeout


def test_default_budget_reaches_schedule_command() -> None:
    """缺省：派发命令的 start_to_close_timeout = 钉扎 360s。"""
    got = asyncio.run(_scheduled_timeout(_payload()))
    assert got == float(_DEFAULT_ACTIVATION_BUDGET_SECONDS)


def test_explicit_budget_changes_schedule_command() -> None:
    """**关键**：显式放大预算后，派发命令真的带上新值。

    这条是「旋钮真的接上了」的行为证据：只断言纯函数会漏掉「忘记接线」。
    """
    got = asyncio.run(_scheduled_timeout(_payload(activation_budget_seconds=1000)))
    assert got == 1000.0
    assert got != float(_DEFAULT_ACTIVATION_BUDGET_SECONDS)


def test_budget_beyond_window_falls_back_in_schedule_command() -> None:
    """预算超出观察窗口时不被采用：命令回落到钉扎值（避免新的静默误杀）。"""
    got = asyncio.run(_scheduled_timeout(_payload(activation_budget_seconds=99999)))
    assert got == float(_DEFAULT_ACTIVATION_BUDGET_SECONDS)


def test_patch_marker_declared() -> None:
    """patch 门常量存在（旧历史经该门逐字保留 360s）。"""
    assert _ACTIVATION_BUDGET_PATCH == "activation-budget-v1"
