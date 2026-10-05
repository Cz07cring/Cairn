"""TM03 部分举证：收集历史后 Replayer 重放；不再打模型/工具 Activity。

真实 temporalio 1.32 `WorkflowEnvironment.start_time_skipping` + `Replayer`；
禁 LEGACY fallback；Workflow 结果不得 marks_goal_done。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from orchestration.client import DEFAULT_TASK_QUEUE, RUNNER_TASK_QUEUE
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from temporalio.workflow import NondeterminismError

# 进程内可变计数：证明首次执行会调用；Replayer 重放不得再 append
_activity_calls: list[str] = []


@activity.defn(name="ensure_goal_delivery")
async def _counting_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    _activity_calls.append("ensure_goal_delivery")
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-tm03",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _counting_list(goal_id: str, owner_epoch: str) -> dict:
    """每轮只报一次就绪工作；第二次返回空（贴近真实语义）。

    原写法每次调用都生成**新的 uuid** 就绪活动，等于建模「Kernel 无限产生新工作」——
    与真实语义不符：活动一经准入即离开 READY，下一次 list 不应再报同一份工作。
    advance-on-ready-v1 引入末段探测 list 后，旧写法会让工作流一路续跑到跳数上限
    （实测 200 跳 ≈ 1200 次活动调用）。改为「首轮报、次轮空」，与演化门
    `_list_once` 同型，也使本文件的 `_EXPECTED_CALL_PREFIX` 断言保持单轮语义。
    """
    already_served = "list_runtime_actions" in _activity_calls
    _activity_calls.append("list_runtime_actions")
    if already_served:
        return {"actions": [], "wait_hint": {"code": "NO_READY_ACTIVITIES"}}
    aid = str(uuid4())
    return {
        "actions": [
            {
                "activity_id": aid,
                "action_id": aid,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="admit_plan_action")
async def _counting_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    _activity_calls.append("admit_plan_action")
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": str(uuid4()),
        "fencing_epoch": "1",
    }


@activity.defn(name="RunActivation")
async def _counting_run_activation(payload: dict) -> dict:
    """计数 stub：若 Replayer 误再执行，列表会变长。"""
    _activity_calls.append("RunActivation")
    assert payload.get("kind") == "PLAN"
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "Harness RunActivation 未接线",
        "kind": payload["kind"],
        "pending_harness": True,
    }


@activity.defn(name="RunActivation")
async def _failing_run_activation(payload: dict) -> dict:
    """可重试错误 stub：Workflow 必须单次投递后转入 Kernel 状态观察。"""
    _activity_calls.append("RunActivation")
    raise RuntimeError(f"activation failed for {payload['activity_id']}")


@activity.defn(name="RunActivation")
async def _nonretryable_failing_run_activation(payload: dict) -> dict:
    """生成旧版失败历史，避免默认无限 retry 阻塞夹具。"""
    _activity_calls.append("RunActivation")
    raise ApplicationError(
        f"legacy activation failed for {payload['activity_id']}",
        non_retryable=True,
    )


@activity.defn(name="observe_activity_status")
async def _counting_observe(activity_id: str) -> dict[str, str]:
    _activity_calls.append("observe_activity_status")
    return {"activity_id": activity_id, "status": "SUCCEEDED", "kind": "PLAN"}


# 同名 GoalWorkflow，但 Activity 顺序故意颠倒 → 历史不兼容
@workflow.defn(name="GoalWorkflow")
class _GoalWorkflowIncompatibleOrder:
    """仅用于 TM03：与已导出历史不兼容的定义（先 list 再 ensure）。"""

    @workflow.run
    async def run(self, payload: dict | None = None) -> dict:
        data = payload or {}
        goal_id = str(data.get("goal_id") or "")
        command_id = str(data.get("command_id") or "")
        await workflow.execute_activity(
            "list_runtime_actions",
            args=[goal_id, "1"],
            start_to_close_timeout=timedelta(seconds=60),
        )
        await workflow.execute_activity(
            "ensure_goal_delivery",
            args=[command_id, goal_id],
            start_to_close_timeout=timedelta(seconds=60),
        )
        return {"ok": True, "marks_goal_done": False}


@workflow.defn(name="GoalWorkflow")
class _GoalWorkflowBeforeB087:
    """B087 前无 patch / 默认 retry 的最小兼容历史生产者。"""

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
            action_ids.append(activity_id)
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


# 末位 `list_runtime_actions` 来自 advance-on-ready-v1（跨活动推进）：本轮有实质进展时
# 先探一次是否仍有就绪工作，再决定是否 Continue-As-New 续跑。旧历史无该 patch 标记
# （见 test_temporal_evolution_gate 的 advance-on-ready-v1 契约）故仍逐字重放。
_EXPECTED_CALL_PREFIX = (
    "ensure_goal_delivery",
    "list_runtime_actions",
    "admit_plan_action",
    "RunActivation",
    "observe_activity_status",
    "list_runtime_actions",
)


async def _execute_once_and_fetch_history(env: WorkflowEnvironment):
    """双队列跑完 GoalWorkflow，返回 (result, handle, history)。"""
    goal_id = str(uuid4())
    command_id = str(uuid4())
    workflow_id = f"goal-{goal_id}"
    async with (
        Worker(
            env.client,
            task_queue=DEFAULT_TASK_QUEUE,
            workflows=[GoalWorkflow],
            activities=[
                _counting_ensure,
                _counting_list,
                _counting_admit,
                _counting_observe,
            ],
        ),
        Worker(
            env.client,
            task_queue=RUNNER_TASK_QUEUE,
            activities=[_counting_run_activation],
        ),
    ):
        handle = await env.client.start_workflow(
            GoalWorkflow.run,
            {
                "command_id": command_id,
                "goal_id": goal_id,
                "owner_epoch": "1",
                "observe_max_ticks": 1,
            },
            id=workflow_id,
            task_queue=DEFAULT_TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
        result = await handle.result()
        history = await handle.fetch_history()
        return result, handle, history, workflow_id, command_id, goal_id


def test_tm03_replayer_does_not_reinvoke_activities():
    """首次执行计数 Activity；Replayer 重放历史不得再打模型/工具 stub。"""

    async def _run() -> None:
        _activity_calls.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            result, _handle, history, *_rest = await _execute_once_and_fetch_history(env)

        assert result["ok"] is True
        assert result["marks_goal_done"] is False
        assert result["pending_harness"] is True
        # 完整控制环：ensure → list → admit → RunActivation → observe
        assert tuple(_activity_calls) == _EXPECTED_CALL_PREFIX
        calls_after_first = list(_activity_calls)

        # Replayer 只重放 Workflow 判定，不调度 Activity → 计数不变
        replayer = Replayer(workflows=[GoalWorkflow])
        replay_result = await replayer.replay_workflow(history)
        assert replay_result.replay_failure is None
        assert _activity_calls == calls_after_first

    asyncio.run(_run())


def test_tm03_run_activation_has_single_temporal_attempt():
    """模型 activation 明确禁用 SDK 默认重试，避免超时后重复计费或副作用。"""

    async def _run() -> None:
        _activity_calls.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            _result, _handle, history, *_rest = await _execute_once_and_fetch_history(env)

        scheduled = [
            event.activity_task_scheduled_event_attributes
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
            and event.activity_task_scheduled_event_attributes.activity_type.name
            == "RunActivation"
        ]
        assert len(scheduled) == 1
        assert scheduled[0].retry_policy.maximum_attempts == 1

    asyncio.run(_run())


def test_tm03_run_activation_failure_is_observed_once_then_reconciled_with_kernel():
    """可重试错误只调用一次；Workflow 继续读取 Kernel 业务终态。"""

    async def _run() -> None:
        _activity_calls.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _counting_ensure,
                        _counting_list,
                        _counting_admit,
                        _counting_observe,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_failing_run_activation],
                ),
            ):
                result = await env.client.execute_workflow(
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

        assert _activity_calls.count("RunActivation") == 1
        # 整段命令序列与其它用例同源；末位 `list_runtime_actions` 是 advance-on-ready-v1
        # 的推进探测（见 _EXPECTED_CALL_PREFIX 注释），非新增的 RunActivation。
        assert tuple(_activity_calls) == _EXPECTED_CALL_PREFIX
        assert result["run_activation_results"][0]["status"] == "FAILED"
        assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
        assert result["marks_goal_done"] is False

    asyncio.run(_run())


def test_tm03_b086_patch_replays_pre_change_history():
    """旧历史无 patch marker 时保持原 Activity 命令与默认 retry。"""

    async def _run() -> None:
        _activity_calls.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[_GoalWorkflowBeforeB087],
                    activities=[
                        _counting_ensure,
                        _counting_list,
                        _counting_admit,
                        _counting_observe,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_counting_run_activation],
                ),
            ):
                handle = await env.client.start_workflow(
                    _GoalWorkflowBeforeB087.run,
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
                await handle.result()
                history = await handle.fetch_history()

        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_tm03_b086_patch_replays_pre_change_failure_history():
    """旧版失败历史仍按失败结束；新代码不追加 Kernel observe 命令。"""

    async def _run() -> None:
        _activity_calls.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[_GoalWorkflowBeforeB087],
                    activities=[
                        _counting_ensure,
                        _counting_list,
                        _counting_admit,
                        _counting_observe,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_nonretryable_failing_run_activation],
                ),
            ):
                handle = await env.client.start_workflow(
                    _GoalWorkflowBeforeB087.run,
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
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
                history = await handle.fetch_history()

        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None
        assert "observe_activity_status" not in _activity_calls

    asyncio.run(_run())


def test_tm03_same_workflow_id_already_started_no_double_run():
    """同 workflow_id + REJECT_DUPLICATE 二次 start → AlreadyStarted；首跑不双计。"""

    async def _run() -> None:
        _activity_calls.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            command_id = str(uuid4())
            workflow_id = f"goal-{goal_id}"
            payload = {
                "command_id": command_id,
                "goal_id": goal_id,
                "owner_epoch": "1",
                "observe_max_ticks": 1,
            }
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _counting_ensure,
                        _counting_list,
                        _counting_admit,
                        _counting_observe,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_counting_run_activation],
                ),
            ):
                handle = await env.client.start_workflow(
                    GoalWorkflow.run,
                    payload,
                    id=workflow_id,
                    task_queue=DEFAULT_TASK_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
                result = await handle.result()
                assert result["ok"] is True
                assert result["marks_goal_done"] is False
                n_after_first = len(_activity_calls)
                assert tuple(_activity_calls) == _EXPECTED_CALL_PREFIX

                # 默认 ALLOW_DUPLICATE 会在完成后新开 run；显式拒绝复用证明 id 幂等
                with pytest.raises(WorkflowAlreadyStartedError):
                    await env.client.start_workflow(
                        GoalWorkflow.run,
                        payload,
                        id=workflow_id,
                        task_queue=DEFAULT_TASK_QUEUE,
                        execution_timeout=timedelta(seconds=30),
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )

                again = await handle.result()
                assert again["command_id"] == result["command_id"]
                assert again["marks_goal_done"] is False
                assert len(_activity_calls) == n_after_first

    asyncio.run(_run())


def test_tm03_incompatible_workflow_change_detected_by_replayer():
    """Activity 顺序变更的同名 Workflow：Replayer 须失败（不兼容被测发现）。"""

    async def _run() -> None:
        _activity_calls.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            _result, _handle, history, *_ = await _execute_once_and_fetch_history(env)
        calls_snapshot = list(_activity_calls)

        bad = Replayer(workflows=[_GoalWorkflowIncompatibleOrder])
        with pytest.raises(NondeterminismError, match="Nondeterminism|does not match"):
            await bad.replay_workflow(history)

        # 失败路径仍不得副作用调用 Activity
        assert _activity_calls == calls_snapshot

    asyncio.run(_run())
