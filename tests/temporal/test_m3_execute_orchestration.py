"""M3：GoalWorkflow admit EXECUTE（patch 门）+ 旧历史重放 + 正向/CAN。

ACTIVATION_SUBMITTED / 观察终态 ≠ Activity 验收 PASS ≠ Goal DONE。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

from orchestration.client import DEFAULT_TASK_QUEUE, RUNNER_TASK_QUEUE
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

_admit_calls: list[str] = []
_run_calls: list[dict] = []
_activity_calls: list[str] = []

_FIXED_EXECUTE_ID = "act-m3-execute-1"


def _reset() -> None:
    _admit_calls.clear()
    _run_calls.clear()
    _activity_calls.clear()


@activity.defn(name="ensure_goal_delivery")
async def _ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    _activity_calls.append("ensure_goal_delivery")
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-m3",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _list_plan_and_execute(goal_id: str, owner_epoch: str) -> dict:
    """旧历史场景：list 同时返回 PLAN + EXECUTE。"""
    _activity_calls.append("list_runtime_actions")
    plan_id = str(uuid4())
    exec_id = str(uuid4())
    return {
        "actions": [
            {
                "activity_id": plan_id,
                "action_id": plan_id,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            },
            {
                "activity_id": exec_id,
                "action_id": exec_id,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "EXECUTE",
            },
        ],
        "wait_hint": None,
    }


@activity.defn(name="list_runtime_actions")
async def _list_execute_only(goal_id: str, owner_epoch: str) -> dict:
    _activity_calls.append("list_runtime_actions")
    return {
        "actions": [
            {
                "activity_id": _FIXED_EXECUTE_ID,
                "action_id": _FIXED_EXECUTE_ID,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "EXECUTE",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="list_runtime_actions")
async def _list_empty_after_first(goal_id: str, owner_epoch: str) -> dict:
    """CAN：首轮返回 EXECUTE；次轮无 READY。"""
    _activity_calls.append("list_runtime_actions")
    if _activity_calls.count("list_runtime_actions") > 1:
        return {
            "actions": [],
            "wait_hint": {
                "code": "NO_READY_ACTIVITIES",
                "message": "当前无 READY 活动，保持等待",
            },
        }
    return await _list_execute_only(goal_id, owner_epoch)


@activity.defn(name="admit_plan_action")
async def _admit_plan(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    _activity_calls.append("admit_plan_action")
    _admit_calls.append(f"PLAN:{activity_id}")
    return {
        "activity_id": activity_id,
        "attempt_id": str(uuid4()),
        "fencing_epoch": "1",
    }


@activity.defn(name="admit_execute_action")
async def _admit_execute(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    _activity_calls.append("admit_execute_action")
    _admit_calls.append(f"EXECUTE:{activity_id}")
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": "attempt-exec-1",
        "fencing_epoch": "1",
    }


@activity.defn(name="RunActivation")
async def _run_activation(payload: dict) -> dict:
    _activity_calls.append("RunActivation")
    _run_calls.append(dict(payload))
    kind = payload.get("kind")
    if kind == "EXECUTE":
        return {
            "status": "ACTIVATION_SUBMITTED",
            "pending_harness": False,
            "kind": "EXECUTE",
            "activity_id": payload["activity_id"],
            "attempt_id": payload.get("attempt_id"),
            "fencing_epoch": payload.get("fencing_epoch"),
            "effect_ids": ["effect-stub-1"],
        }
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "Harness PLAN stub",
        "kind": kind,
        "pending_harness": True,
    }


@activity.defn(name="observe_activity_status")
async def _observe(activity_id: str) -> dict[str, str]:
    _activity_calls.append("observe_activity_status")
    return {"activity_id": activity_id, "status": "SUCCEEDED", "kind": "EXECUTE"}


@activity.defn(name="observe_activity_status")
async def _observe_always_running(activity_id: str) -> dict[str, str]:
    """首跑耗尽 tick → OBSERVE_TIMEOUT → CAN。"""
    _activity_calls.append("observe_activity_status")
    return {"activity_id": activity_id, "status": "RUNNING", "kind": "EXECUTE"}


@activity.defn(name="observe_carried_activation_activity_status")
async def _observe_carried(
    goal_id: str, owner_epoch: str, activity_id: str
) -> dict[str, str]:
    _activity_calls.append("observe_carried_activation_activity_status")
    assert goal_id and owner_epoch == "1"
    return {"activity_id": activity_id, "status": "SUCCEEDED", "kind": "EXECUTE"}


@workflow.defn(name="GoalWorkflow")
class _GoalWorkflowIgnoreExecuteInList:
    """合入 M3 前：list 可含 EXECUTE，但只 admit PLAN（冻结历史生产者）。"""

    @workflow.run
    async def run(self, payload: dict | None = None) -> dict:
        data = payload or {}
        command_id = str(data.get("command_id") or "")
        goal_id = str(data.get("goal_id") or "")
        owner_epoch = str(data.get("owner_epoch") or "1")
        delivery = await workflow.execute_activity(
            "ensure_goal_delivery",
            args=[command_id, goal_id],
            start_to_close_timeout=timedelta(seconds=60),
        )
        actions_result = await workflow.execute_activity(
            "list_runtime_actions",
            args=[goal_id, owner_epoch],
            start_to_close_timeout=timedelta(seconds=60),
        )
        action_ids: list[str] = []
        admitted_activity_ids: list[str] = []
        admitted_attempt_ids: list[str] = []
        run_activation_results: list[dict] = []
        observe_activity_ids: list[str] = []
        for action in actions_result.get("actions") or []:
            activity_id = str(action.get("activity_id") or "")
            if not activity_id:
                continue
            action_ids.append(activity_id)
            if action.get("kind") != "PLAN":
                continue
            lease = await workflow.execute_activity(
                "admit_plan_action",
                args=[activity_id, f"goal-wf-admit:{activity_id}"],
                start_to_close_timeout=timedelta(seconds=60),
            )
            admitted_activity_ids.append(activity_id)
            admitted_attempt_ids.append(str(lease.get("attempt_id") or ""))
            observe_activity_ids.append(activity_id)
            result = await workflow.execute_activity(
                "RunActivation",
                args=[
                    {
                        "activity_id": activity_id,
                        "attempt_id": str(lease.get("attempt_id") or ""),
                        "goal_id": goal_id,
                        "kind": "PLAN",
                        "fencing_epoch": str(lease.get("fencing_epoch") or ""),
                        "owner_epoch": owner_epoch,
                    }
                ],
                task_queue=RUNNER_TASK_QUEUE,
                start_to_close_timeout=timedelta(seconds=360),
            )
            run_activation_results.append(result)
        plan_terminal_statuses = []
        for activity_id in observe_activity_ids:
            observed = await workflow.execute_activity(
                "observe_activity_status",
                args=[activity_id],
                start_to_close_timeout=timedelta(seconds=30),
            )
            plan_terminal_statuses.append(
                {"activity_id": activity_id, "status": observed["status"]}
            )
        return {
            "ok": True,
            "command_id": str(delivery.get("command_id") or command_id),
            "workflow_id": delivery.get("workflow_id"),
            "run_id": delivery.get("run_id"),
            "delivery_status": delivery.get("delivery_status"),
            "action_ids": action_ids,
            "admitted_activity_ids": admitted_activity_ids,
            "admitted_attempt_ids": admitted_attempt_ids,
            "run_activation_results": run_activation_results,
            "pending_harness": True,
            "plan_terminal_statuses": plan_terminal_statuses,
            "generation": 0,
            "prior_run_id": None,
            "command_ids_consumed": [],
            "continued_as_new": False,
            "marks_goal_done": False,
        }


def test_m3_patch_replays_history_that_ignored_execute_in_list():
    """旧历史 list 含 EXECUTE 但未 admit；新代码重放不得新增 execute 命令。"""

    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[_GoalWorkflowIgnoreExecuteInList],
                    activities=[_ensure, _list_plan_and_execute, _admit_plan, _observe],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_run_activation],
                ),
            ):
                handle = await env.client.start_workflow(
                    _GoalWorkflowIgnoreExecuteInList.run,
                    {
                        "command_id": str(uuid4()),
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "observe_max_ticks": 1,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=DEFAULT_TASK_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )
                result = await handle.result()
                history = await handle.fetch_history()

        assert len(result["action_ids"]) == 2
        assert len(result["admitted_activity_ids"]) == 1
        assert "admit_execute_action" not in _activity_calls

        _reset()
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None
        # Replayer 不执行 Activity；调用表应仍为空
        assert _activity_calls == []
        assert _admit_calls == []

    asyncio.run(_run())


def test_m3_execute_admits_once_submitted_not_goal_done():
    """新 Run：EXECUTE 只 admit 一次；ACTIVATION_SUBMITTED ≠ Goal DONE。"""

    async def _run() -> dict:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _ensure,
                        _list_execute_only,
                        _admit_execute,
                        _observe,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_run_activation],
                ),
            ):
                return await env.client.execute_workflow(
                    GoalWorkflow.run,
                    {
                        "command_id": str(uuid4()),
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "observe_max_ticks": 1,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=DEFAULT_TASK_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["marks_goal_done"] is False
    assert result["admitted_activity_ids"] == [_FIXED_EXECUTE_ID]
    assert _admit_calls == [f"EXECUTE:{_FIXED_EXECUTE_ID}"]
    assert len(_run_calls) == 1
    assert _run_calls[0]["kind"] == "EXECUTE"
    act = result["run_activation_results"][0]
    assert act["status"] == "ACTIVATION_SUBMITTED"
    assert act["effect_ids"] == ["effect-stub-1"]
    assert act["kind"] == "EXECUTE"
    assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
    assert result["pending_harness"] is False


def test_m3_execute_continue_as_new_uses_activation_observer():
    """EXECUTE 在途 CAN：次轮 skip_admit，走 observe_carried_activation。"""

    async def _run() -> dict:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _ensure,
                        _list_empty_after_first,
                        _admit_execute,
                        _observe_always_running,
                        _observe_carried,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_run_activation],
                ),
            ):
                handle = await env.client.start_workflow(
                    GoalWorkflow.run,
                    {
                        "command_id": str(uuid4()),
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "observe_max_ticks": 2,
                        "enable_continue_as_new": True,
                        "continue_as_new_on_observe_timeout": True,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=DEFAULT_TASK_QUEUE,
                    execution_timeout=timedelta(seconds=60),
                )
                return await handle.result()

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["marks_goal_done"] is False
    assert result["generation"] >= 1
    assert _admit_calls == [f"EXECUTE:{_FIXED_EXECUTE_ID}"]
    assert "observe_carried_activation_activity_status" in _activity_calls
    assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
