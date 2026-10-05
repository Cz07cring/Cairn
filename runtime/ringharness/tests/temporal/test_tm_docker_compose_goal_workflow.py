"""docker compose 本机 Temporal（auto-setup digest 钉扎）联调 GoalWorkflow。

与 start_local 互补：本文件验的是 deploy/temporal 候选 Server，不是 SDK CLI。
未设置 RING_TEMPORAL_TARGET 且 127.0.0.1:7233 不可连 → skip（不 Fake、不 LEGACY）。
observe ≠ acceptance；不得 marks_goal_done。
"""

from __future__ import annotations

import asyncio
import os
import socket
from uuid import uuid4

import pytest
from orchestration import RealTemporalClient
from orchestration.client import DEFAULT_TASK_QUEUE, RUNNER_TASK_QUEUE
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

_observe_ticks: dict[str, int] = {}


def _temporal_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_target() -> str | None:
    env_target = (os.environ.get("RING_TEMPORAL_TARGET") or "").strip()
    if env_target:
        return env_target
    if _temporal_reachable("127.0.0.1", 7233):
        return "127.0.0.1:7233"
    return None


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-stub-docker",
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
    n = _observe_ticks.get(activity_id, 0) + 1
    _observe_ticks[activity_id] = n
    status = "SUCCEEDED" if n >= 2 else "RUNNING"
    return {"activity_id": activity_id, "status": status, "kind": "PLAN"}


def test_docker_compose_temporal_goal_workflow_roundtrip():
    """本机 compose Temporal + RealTemporalClient + 双队列 Worker；不标 DONE。"""
    target = _resolve_target()
    if not target:
        pytest.skip("本机 Temporal（RING_TEMPORAL_TARGET 或 127.0.0.1:7233）不可连")

    host, _, port_s = target.partition(":")
    port = int(port_s or "7233")
    if not _temporal_reachable(host, port):
        pytest.skip(f"Temporal target 不可达: {target}")

    _observe_ticks.clear()
    goal_id = str(uuid4())
    command_id = str(uuid4())
    workflow_id = f"goal-{goal_id}"

    async def _run() -> dict:
        client = await Client.connect(target)
        control_activities = [
            _stub_ensure,
            _stub_list,
            _stub_admit,
            _stub_observe,
        ]
        async with (
            Worker(
                client,
                task_queue=DEFAULT_TASK_QUEUE,
                workflows=[GoalWorkflow],
                activities=control_activities,
            ),
            Worker(
                client,
                task_queue=RUNNER_TASK_QUEUE,
                activities=[_stub_run_activation],
            ),
        ):
            # RealTemporalClient 同步 ensure_started；在线程外跑以免嵌套 loop
            real = RealTemporalClient(target=target)
            run_id = await asyncio.to_thread(
                real.ensure_started,
                workflow_id,
                command_id=command_id,
                goal_id=goal_id,
                owner_epoch="1",
            )
            assert run_id
            handle = client.get_workflow_handle(workflow_id)
            return await asyncio.wait_for(handle.result(), timeout=60)

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["marks_goal_done"] is False
    assert result.get("pending_harness") is True
    assert result.get("plan_terminal_statuses")
    assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
