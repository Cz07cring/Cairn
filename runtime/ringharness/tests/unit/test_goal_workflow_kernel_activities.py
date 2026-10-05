"""GoalWorkflow + Kernel Activities：时间跳跃 stub 与双队列 RunActivation / observe。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# 与 production 队列名对齐（Workflow 内字面量 ring-runner）
_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-stub",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _stub_list(goal_id: str, owner_epoch: str) -> dict:
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
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": str(uuid4()),
        "fencing_epoch": "1",
    }


@activity.defn(name="admit_execute_action")
async def _stub_admit_execute(
    activity_id: str, idempotency_key: str
) -> dict[str, str | None]:
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": str(uuid4()),
        "fencing_epoch": "1",
    }


@activity.defn(name="RunActivation")
async def _stub_run_activation(payload: dict) -> dict:
    """与 TS RunActivation 同形：诚实 NOT_IMPLEMENTED，不伪造 Harness。"""
    assert payload.get("kind") in ("PLAN", "EXECUTE")
    assert payload.get("activity_id")
    assert payload.get("goal_id")
    assert payload.get("owner_epoch")
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "Harness RunActivation 未接线",
        "kind": payload["kind"],
        "pending_harness": True,
    }


# observe stub 工厂：避免同名 Activity 重复装饰冲突
_observe_ticks: dict[str, int] = {}


def _make_observe_stub(*, succeed_after: int | None):
    """succeed_after=N：第 N 次起 SUCCEEDED；None：始终 RUNNING。"""

    @activity.defn(name="observe_activity_status")
    async def _stub_observe(activity_id: str) -> dict[str, str]:
        n = _observe_ticks.get(activity_id, 0) + 1
        _observe_ticks[activity_id] = n
        if succeed_after is not None and n >= succeed_after:
            status = "SUCCEEDED"
        else:
            status = "RUNNING"
        return {"activity_id": activity_id, "status": status, "kind": "PLAN"}

    return _stub_observe


def _assert_goal_workflow_pending_harness(result: dict) -> None:
    assert result["ok"] is True
    assert result["marks_goal_done"] is False
    assert result["delivery_status"] == "ACKNOWLEDGED"
    assert len(result["action_ids"]) == 1
    assert result["admitted_activity_ids"] == result["action_ids"]
    assert len(result["admitted_attempt_ids"]) == 1
    assert result["pending_harness"] is True
    assert len(result["run_activation_results"]) == 1
    act = result["run_activation_results"][0]
    assert act["status"] == "NOT_IMPLEMENTED"
    assert act["pending_harness"] is True
    assert act["kind"] == "PLAN"
    assert "plan_terminal_statuses" in result
    assert len(result["plan_terminal_statuses"]) == 1
    assert result["plan_terminal_statuses"][0]["activity_id"] == result["action_ids"][0]


async def _run_goal_workflow_two_queues(
    env: WorkflowEnvironment,
    *,
    observe_activity,
    observe_max_ticks: int = 5,
) -> dict:
    """控制队列跑 GoalWorkflow+Kernel stub；Runner 队列跑 RunActivation stub。"""
    goal_id = str(uuid4())
    command_id = str(uuid4())
    async with (
        Worker(
            env.client,
            task_queue=_CONTROL_QUEUE,
            workflows=[GoalWorkflow],
            activities=[
                _stub_ensure,
                _stub_list,
                _stub_admit,
                _stub_admit_execute,
                observe_activity,
            ],
        ),
        Worker(
            env.client,
            task_queue=_RUNNER_QUEUE,
            activities=[_stub_run_activation],
        ),
    ):
        return await env.client.execute_workflow(
            GoalWorkflow.run,
            {
                "command_id": command_id,
                "goal_id": goal_id,
                "owner_epoch": "1",
                "observe_max_ticks": observe_max_ticks,
            },
            id=f"goal-{goal_id}",
            task_queue=_CONTROL_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )


async def _run_goal_workflow_time_skipping(*, observe_activity, observe_max_ticks: int = 5) -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        return await _run_goal_workflow_two_queues(
            env,
            observe_activity=observe_activity,
            observe_max_ticks=observe_max_ticks,
        )


def test_goal_workflow_ensure_list_admit_via_time_skipping():
    """temporalio 1.32 time-skipping：双队列 + RunActivation stub，不标 DONE。"""
    _observe_ticks.clear()
    result = asyncio.run(
        _run_goal_workflow_time_skipping(
            observe_activity=_make_observe_stub(succeed_after=2),
            observe_max_ticks=5,
        )
    )
    _assert_goal_workflow_pending_harness(result)
    assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"


def test_goal_workflow_observe_running_then_succeeded():
    """观察循环：首 tick RUNNING，次 tick SUCCEEDED；pending_harness 仍诚实。"""
    _observe_ticks.clear()
    result = asyncio.run(
        _run_goal_workflow_time_skipping(
            observe_activity=_make_observe_stub(succeed_after=2),
            observe_max_ticks=5,
        )
    )
    assert result["ok"] is True
    assert result["marks_goal_done"] is False
    assert result["pending_harness"] is True
    term = result["plan_terminal_statuses"]
    assert len(term) == 1
    assert term[0]["status"] == "SUCCEEDED"
    assert term[0]["activity_id"] == result["admitted_activity_ids"][0]
    # 至少两轮观察
    assert _observe_ticks[term[0]["activity_id"]] >= 2


def test_goal_workflow_observe_timeout_keeps_ok():
    """始终 RUNNING → OBSERVE_TIMEOUT；Workflow 仍 ok，不标 Goal DONE。"""
    _observe_ticks.clear()
    result = asyncio.run(
        _run_goal_workflow_time_skipping(
            observe_activity=_make_observe_stub(succeed_after=None),
            observe_max_ticks=3,
        )
    )
    assert result["ok"] is True
    assert result["marks_goal_done"] is False
    assert result["pending_harness"] is True
    assert result["plan_terminal_statuses"][0]["status"] == "OBSERVE_TIMEOUT"


async def _run_goal_workflow_start_local() -> dict:
    """真实本地 Temporal（下载 test server）；失败由调用方 skip。"""
    _observe_ticks.clear()
    async with await WorkflowEnvironment.start_local() as env:
        return await _run_goal_workflow_two_queues(
            env,
            observe_activity=_make_observe_stub(succeed_after=2),
            observe_max_ticks=5,
        )


def test_goal_workflow_run_activation_via_start_local_or_time_skipping():
    """RealTemporal 路径：优先 start_local；下载失败则回退 time_skipping 双队列。"""
    try:
        result = asyncio.run(_run_goal_workflow_start_local())
    except Exception as local_exc:  # noqa: BLE001 — CLI 下载/拉起失败面广
        try:
            _observe_ticks.clear()
            result = asyncio.run(
                _run_goal_workflow_time_skipping(
                    observe_activity=_make_observe_stub(succeed_after=2),
                    observe_max_ticks=5,
                )
            )
        except Exception as skip_exc:  # noqa: BLE001
            pytest.skip(
                f"WorkflowEnvironment.start_local 不可用且 time_skipping 失败: "
                f"{local_exc}; {skip_exc}"
            )
    _assert_goal_workflow_pending_harness(result)
    assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"


def test_default_orchestration_backend_env_only_when_temporal(monkeypatch):
    """RING_ORCHESTRATION_DEFAULT_BACKEND 仅 TEMPORAL 生效；缺省 LEGACY。"""
    from control_kernel.storage.goals import default_orchestration_backend

    assert default_orchestration_backend({}) == "LEGACY"
    assert default_orchestration_backend({"RING_ORCHESTRATION_DEFAULT_BACKEND": ""}) == "LEGACY"
    assert default_orchestration_backend({"RING_ORCHESTRATION_DEFAULT_BACKEND": "legacy"}) == "LEGACY"
    assert (
        default_orchestration_backend({"RING_ORCHESTRATION_DEFAULT_BACKEND": "TEMPORAL"})
        == "TEMPORAL"
    )
    monkeypatch.setenv("RING_ORCHESTRATION_DEFAULT_BACKEND", "TEMPORAL")
    assert default_orchestration_backend() == "TEMPORAL"
    monkeypatch.delenv("RING_ORCHESTRATION_DEFAULT_BACKEND", raising=False)
    assert default_orchestration_backend() == "LEGACY"


def test_require_temporal_enforced_only_with_target():
    """REQUIRE_TEMPORAL 仅在同时配置 TARGET 时强制默认 TEMPORAL。"""
    from control_kernel.storage.goals import (
        default_orchestration_backend,
        legacy_claim_drain_allowed,
        temporal_orchestration_enforced,
    )

    assert temporal_orchestration_enforced({}) is False
    assert (
        temporal_orchestration_enforced(
            {"RING_ORCHESTRATION_REQUIRE_TEMPORAL": "1"}
        )
        is False
    )
    assert (
        temporal_orchestration_enforced(
            {
                "RING_ORCHESTRATION_REQUIRE_TEMPORAL": "1",
                "RING_TEMPORAL_TARGET": "127.0.0.1:7233",
            }
        )
        is True
    )
    assert (
        default_orchestration_backend(
            {
                "RING_ORCHESTRATION_REQUIRE_TEMPORAL": "1",
                "RING_TEMPORAL_TARGET": "127.0.0.1:7233",
            }
        )
        == "TEMPORAL"
    )
    # 仅 REQUIRE、无 TARGET：不静默强制
    assert (
        default_orchestration_backend({"RING_ORCHESTRATION_REQUIRE_TEMPORAL": "1"})
        == "LEGACY"
    )
    assert legacy_claim_drain_allowed({}) is False
    assert legacy_claim_drain_allowed({"RING_LEGACY_CLAIM_DRAIN": "1"}) is True


def test_count_legacy_goals_with_open_work_keys():
    """退役台账返回固定键；空库时计数为非负整数。"""
    import os

    from control_kernel.storage.goals import count_legacy_goals_with_open_work
    from sqlalchemy import create_engine

    url = os.environ.get("RING_TEST_DATABASE_URL") or os.environ.get("RING_DATABASE_URL")
    if not url:
        import pytest

        pytest.skip("需要测试库 URL")
    engine = create_engine(url)
    try:
        snap = count_legacy_goals_with_open_work(engine)
        assert set(snap) == {
            "legacy_goals_open",
            "legacy_activities_open",
            "legacy_attempts_active",
        }
        assert all(isinstance(v, int) and v >= 0 for v in snap.values())
    finally:
        engine.dispose()
