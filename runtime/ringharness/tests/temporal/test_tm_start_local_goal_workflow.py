"""第六十七批：真 Temporal test server（start_local）联调 GoalWorkflow。

不依赖 docker Hub auto-setup；下载/网络失败则 pytest.skip（不回退 LEGACY，
也不在本文件静默改走 FakeTemporalClient）。
observe ≠ acceptance；Workflow 结果不得 marks_goal_done。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from orchestration import RealTemporalClient
from orchestration.client import DEFAULT_TASK_QUEUE, RUNNER_TASK_QUEUE
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# observe stub 按 activity_id 计数（单测进程内共享，每次用例前 clear）
_observe_ticks: dict[str, int] = {}


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-stub-local",
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


@activity.defn(name="RunActivation")
async def _stub_run_activation(payload: dict) -> dict:
    """与 TS RunActivation 同形：诚实 NOT_IMPLEMENTED，不伪造 Harness。"""
    assert payload.get("kind") == "PLAN"
    assert payload.get("activity_id")
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "Harness RunActivation 未接线",
        "kind": payload["kind"],
        "pending_harness": True,
    }


@activity.defn(name="observe_activity_status")
async def _stub_observe(activity_id: str) -> dict[str, str]:
    """第 2 次起 SUCCEEDED；start_local 无 time-skip，观察轮次须小。"""
    n = _observe_ticks.get(activity_id, 0) + 1
    _observe_ticks[activity_id] = n
    status = "SUCCEEDED" if n >= 2 else "RUNNING"
    return {"activity_id": activity_id, "status": status, "kind": "PLAN"}


def _target_from_env(env: WorkflowEnvironment) -> str:
    """从 start_local 已连接 Client 取出 host:port，供 RealTemporalClient 指向。"""
    return str(env.client.service_client.config.target_host)


async def _run_goal_workflow_on_start_local() -> dict:
    """拉起 CLI dev server + 双 Worker；经 RealTemporalClient 启动并取结果。"""
    _observe_ticks.clear()
    try:
        env = await WorkflowEnvironment.start_local()
    except Exception as exc:  # noqa: BLE001 — 下载 CLI / 端口 / 启动面广
        pytest.skip(
            f"WorkflowEnvironment.start_local 不可用（下载/网络/本机进程失败）: {exc}"
        )

    async with env:
        target = _target_from_env(env)
        # 证明 RealTemporalClient 指向同一 test server（非 Fake）
        real = RealTemporalClient(target=target, namespace="default")
        connected = await Client.connect(target, namespace="default")
        assert connected.service_client.config.target_host == target

        goal_id = str(uuid4())
        command_id = str(uuid4())
        workflow_id = f"goal-{goal_id}"

        async with (
            Worker(
                env.client,
                task_queue=DEFAULT_TASK_QUEUE,
                workflows=[GoalWorkflow],
                activities=[_stub_ensure, _stub_list, _stub_admit, _stub_observe],
            ),
            Worker(
                env.client,
                task_queue=RUNNER_TASK_QUEUE,
                activities=[_stub_run_activation],
            ),
        ):
            # 异步路径对齐 RealTemporalClient.ensure_started（避免内层 asyncio.run）
            run_id = await real._ensure_started_async(
                workflow_id,
                command_id=command_id,
                goal_id=goal_id,
                owner_epoch="1",
            )
            assert run_id
            handle = env.client.get_workflow_handle(workflow_id, run_id=run_id)
            # start_local 无时间跳跃；observe 第 2 tick SUCCEEDED，约 1s 真实 sleep
            return await asyncio.wait_for(handle.result(), timeout=90.0)


def test_goal_workflow_via_start_local_real_temporal_server():
    """真 Temporal SDK test server：GoalWorkflow + stub Kernel/RunActivation。

    失败关闭：start_local 拉不起则 skip；禁止 Fake / LEGACY 冒充联调通过。
    """
    result = asyncio.run(_run_goal_workflow_on_start_local())
    assert result["ok"] is True
    assert result["marks_goal_done"] is False
    assert result["pending_harness"] is True or bool(result.get("plan_terminal_statuses"))
    assert "plan_terminal_statuses" in result
    assert len(result["plan_terminal_statuses"]) >= 1
    assert result["delivery_status"] == "ACKNOWLEDGED"
    assert len(result["admitted_activity_ids"]) == 1
    act = result["run_activation_results"][0]
    assert act["pending_harness"] is True
    assert act["status"] == "NOT_IMPLEMENTED"
    # 观察库内终态 ≠ Goal DONE
    assert result["plan_terminal_statuses"][0]["status"] in {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "OBSERVE_TIMEOUT",
    }
