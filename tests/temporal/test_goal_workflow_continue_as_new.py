"""GoalWorkflow 生产 Continue-As-New 水位（observe-timeout）举证。

enable_continue_as_new + 首跑 OBSERVE_TIMEOUT → CAN；次跑 skip_admit 仅观察至 SUCCEEDED。
禁 LEGACY；marks_goal_done 恒为 False。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"

# 跨 CAN 固定 activity_id，便于 skip_admit 命中
_FIXED_ACTIVITY_ID = "act-can-waterline-1"

_admit_calls: list[str] = []
_run_activation_calls: list[str] = []
_observe_calls: list[str] = []
_list_calls: list[str] = []


def _reset_counters() -> None:
    _admit_calls.clear()
    _run_activation_calls.clear()
    _observe_calls.clear()
    _list_calls.clear()


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-can-stub",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _stub_list_fixed(goal_id: str, owner_epoch: str) -> dict:
    """始终返回同一 PLAN activity_id（跨 CAN 稳定）。"""
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


@activity.defn(name="list_runtime_actions")
async def _stub_list_ready_only_once(goal_id: str, owner_epoch: str) -> dict:
    """模拟真实 Kernel：首 Run 返回 READY；准入后新 Run 不再返回 RUNNING。"""
    _list_calls.append(goal_id)
    if len(_list_calls) > 1:
        return {
            "actions": [],
            "wait_hint": {
                "code": "NO_READY_ACTIVITIES",
                "message": "当前无 READY 活动，保持等待",
            },
        }
    return await _stub_list_fixed(goal_id, owner_epoch)


@activity.defn(name="admit_plan_action")
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    _admit_calls.append(activity_id)
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": "attempt-can-1",
        "fencing_epoch": "1",
    }


@activity.defn(name="RunActivation")
async def _stub_run_activation(payload: dict) -> dict:
    _run_activation_calls.append(str(payload.get("activity_id") or ""))
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "Harness RunActivation 未接线",
        "kind": payload["kind"],
        "pending_harness": True,
    }


@activity.defn(name="observe_activity_status")
async def _stub_observe_then_succeed(activity_id: str) -> dict[str, str]:
    """首跑耗尽 tick 保持 RUNNING；CAN 后（已有 admit）再观察则 SUCCEEDED。"""
    _observe_calls.append(activity_id)
    # 首跑：admit 一次 + observe_max_ticks 次 RUNNING → CAN
    # 次跑：skip admit，下一次 observe 起 SUCCEEDED
    if len(_admit_calls) >= 1 and len(_observe_calls) > 2:
        status = "SUCCEEDED"
    else:
        status = "RUNNING"
    return {"activity_id": activity_id, "status": status, "kind": "PLAN"}


@activity.defn(name="observe_carried_plan_activity_status")
async def _stub_observe_carried_then_succeed(
    goal_id: str,
    owner_epoch: str,
    activity_id: str,
) -> dict[str, str]:
    assert goal_id
    assert owner_epoch == "1"
    return await _stub_observe_then_succeed(activity_id)


@activity.defn(name="observe_carried_activation_activity_status")
async def _stub_observe_carried_activation_then_succeed(
    goal_id: str,
    owner_epoch: str,
    activity_id: str,
) -> dict[str, str]:
    """m3 patch 新 Run 的 CAN 观察入口（PLAN/EXECUTE）。"""
    return await _stub_observe_carried_then_succeed(goal_id, owner_epoch, activity_id)


def test_goal_workflow_continue_as_new_on_observe_timeout_skip_admit():
    """CAN 水位：超时后续跑 skip_admit，仅观察至 SUCCEEDED；不标 DONE。"""

    async def _run() -> None:
        _reset_counters()
        command_id = f"cmd-can-{uuid4()}"
        goal_id = str(uuid4())
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_list_fixed,
                        _stub_admit,
                        _stub_observe_then_succeed,
                        _stub_observe_carried_then_succeed,
                        _stub_observe_carried_activation_then_succeed,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run_activation],
                ),
            ):
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    {
                        "command_id": command_id,
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "observe_max_ticks": 2,
                        "enable_continue_as_new": True,
                        "continue_as_new_on_observe_timeout": True,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=60),
                )

        assert result["ok"] is True
        assert result["marks_goal_done"] is False
        assert result["continued_as_new"] is False
        assert int(result["generation"]) >= 1
        assert command_id in result["command_ids_consumed"]
        assert result["prior_run_id"]  # CAN 携带上一 run
        # 次跑 skip_admit：admit / RunActivation 各仅一次（首跑）
        assert _admit_calls == [_FIXED_ACTIVITY_ID]
        assert _run_activation_calls == [_FIXED_ACTIVITY_ID]
        assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
        assert result["plan_terminal_statuses"][0]["activity_id"] == _FIXED_ACTIVITY_ID

    asyncio.run(_run())


def test_goal_workflow_continue_as_new_observes_carried_running_activity():
    """CAN 后 Kernel 仅返回 READY 时，Workflow 仍续观测已准入的 RUNNING Activity。"""

    async def _run() -> None:
        _reset_counters()
        command_id = f"cmd-can-running-{uuid4()}"
        goal_id = str(uuid4())
        async with (
            await WorkflowEnvironment.start_time_skipping() as env,
            Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[
                    _stub_ensure,
                    _stub_list_ready_only_once,
                    _stub_admit,
                    _stub_observe_then_succeed,
                    _stub_observe_carried_then_succeed,
                    _stub_observe_carried_activation_then_succeed,
                ],
            ),
            Worker(
                env.client,
                task_queue=_RUNNER_QUEUE,
                activities=[_stub_run_activation],
            ),
        ):
            result = await env.client.execute_workflow(
                GoalWorkflow.run,
                {
                    "command_id": command_id,
                    "goal_id": goal_id,
                    "owner_epoch": "1",
                    "observe_max_ticks": 2,
                    "enable_continue_as_new": True,
                    "continue_as_new_on_observe_timeout": True,
                },
                id=f"goal-{goal_id}",
                task_queue=_CONTROL_QUEUE,
                execution_timeout=timedelta(seconds=60),
            )

        assert len(_list_calls) >= 2
        assert _admit_calls == [_FIXED_ACTIVITY_ID]
        assert _run_activation_calls == [_FIXED_ACTIVITY_ID]
        assert result["generation"] >= 1
        assert result["plan_terminal_statuses"] == [
            {"activity_id": _FIXED_ACTIVITY_ID, "status": "SUCCEEDED"}
        ]
        assert result["marks_goal_done"] is False

    asyncio.run(_run())


def test_goal_workflow_continue_as_new_carries_activation_budget():
    """can-budget-carry-v1：CAN 续跑载荷必须透传 activation_budget_seconds。

    完整行为证据（含 Replayer self-replay）见
    tests/temporal/test_temporal_evolution_gate.py::test_can_budget_carry_new_history_carries_budget。
    """
    from tests.temporal.test_temporal_evolution_gate import (
        test_can_budget_carry_new_history_carries_budget as _evidence,
    )

    _evidence()
