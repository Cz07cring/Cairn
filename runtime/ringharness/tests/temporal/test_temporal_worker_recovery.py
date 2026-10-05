"""Worker 失联后新 Worker 接续，不标 DONE。

证明 Temporal 保留 history：Worker1 在首轮 observe 失败后退出，
Worker2 接续 Activity 重试至 SUCCEEDED。
使用 `WorkflowEnvironment.start_local()`（真 CLI dev server）；
`start_time_skipping` 在 Worker 空窗时会卡死 RUNNING，故不用。
禁 LEGACY；marks_goal_done 恒为 False。
下载/拉起失败 → pytest.skip，不 Fake、不冒充通过。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"

_FIXED_ACTIVITY_ID = "act-worker-recovery-1"
_observe_ticks: dict[str, int] = {}
_first_observe_seen: asyncio.Event | None = None


def _reset() -> None:
    global _first_observe_seen
    _observe_ticks.clear()
    _first_observe_seen = asyncio.Event()


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-recovery-stub",
        "delivery_status": "ACKNOWLEDGED",
    }


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
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": "attempt-recovery-1",
        "fencing_epoch": "1",
    }


@activity.defn(name="RunActivation")
async def _stub_run_activation(payload: dict) -> dict:
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "Harness RunActivation 未接线",
        "kind": payload["kind"],
        "pending_harness": True,
    }


@activity.defn(name="observe_activity_status")
async def _stub_observe_fail_then_succeed(activity_id: str) -> dict[str, str]:
    """首轮失败模拟失联中断；Worker2 接续重试后 SUCCEEDED。"""
    n = _observe_ticks.get(activity_id, 0) + 1
    _observe_ticks[activity_id] = n
    if n == 1:
        assert _first_observe_seen is not None
        _first_observe_seen.set()
        raise RuntimeError("模拟 Worker1 失联中断")
    return {"activity_id": activity_id, "status": "SUCCEEDED", "kind": "PLAN"}


def test_temporal_worker_recovery_after_worker_loss():
    """Worker1 退出后 Worker2 接续 history，不标 DONE。"""

    async def _run() -> None:
        _reset()
        assert _first_observe_seen is not None
        command_id = str(uuid4())
        goal_id = str(uuid4())
        workflow_id = f"goal-{goal_id}"
        payload = {
            "command_id": command_id,
            "goal_id": goal_id,
            "owner_epoch": "1",
            "observe_max_ticks": 10,
            "enable_continue_as_new": False,
        }
        control_activities = [
            _stub_ensure,
            _stub_list,
            _stub_admit,
            _stub_observe_fail_then_succeed,
        ]

        try:
            env_cm = await WorkflowEnvironment.start_local()
        except Exception as exc:  # noqa: BLE001 — 下载/端口失败时 skip
            pytest.skip(f"Temporal start_local 不可用: {exc}")

        async with env_cm as env:
            assert env.client is not None
            handle = await env.client.start_workflow(
                GoalWorkflow.run,
                payload,
                id=workflow_id,
                task_queue=_CONTROL_QUEUE,
                execution_timeout=timedelta(seconds=60),
            )

            # Worker1：推进到首轮 observe 失败后关闭
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=control_activities,
                    graceful_shutdown_timeout=timedelta(milliseconds=500),
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run_activation],
                    graceful_shutdown_timeout=timedelta(milliseconds=500),
                ),
            ):
                await asyncio.wait_for(_first_observe_seen.wait(), timeout=15)
                # 给失败回传一点时间，再关 Worker1
                await asyncio.sleep(0.3)
                ticks_after_w1 = _observe_ticks[_FIXED_ACTIVITY_ID]
                assert ticks_after_w1 >= 1

            # Worker2：接续 history；observe 重试 → SUCCEEDED
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=control_activities,
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run_activation],
                ),
            ):
                result = await asyncio.wait_for(handle.result(), timeout=45)

        assert result["ok"] is True
        assert result["marks_goal_done"] is False
        assert result["continued_as_new"] is False
        assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
        assert _observe_ticks[_FIXED_ACTIVITY_ID] >= 2
        assert _observe_ticks[_FIXED_ACTIVITY_ID] > ticks_after_w1

    asyncio.run(_run())
