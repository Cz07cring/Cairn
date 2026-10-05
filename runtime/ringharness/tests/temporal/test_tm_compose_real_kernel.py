"""真实 PG + compose Temporal + 真 Kernel Activities 联调。

路径：TEMPORAL START → Relay(RealTemporalClient) → GoalWorkflow
→ ensure/list/admit（真 Kernel）→ RunActivation stub（写库 SUCCEEDED 以便观察）
→ observe；断言 marks_goal_done=False、backend 仍 TEMPORAL、全局 claim 空。

本机 127.0.0.1:7233 或 RING_TEMPORAL_TARGET 不可连 → skip（不 Fake）。
"""

from __future__ import annotations

import asyncio
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from control_kernel.storage.goals import (
    count_legacy_goals_with_open_work,
    set_goal_orchestration_backend,
)
from control_kernel.storage.orchestration import get_binding_for_goal, get_delivery_for_command
from orchestration import OrchestrationBindingContent, RealTemporalClient, ensure_workflow
from orchestration.client import DEFAULT_TASK_QUEUE, RUNNER_TASK_QUEUE
from orchestration.kernel_activities import (
    admit_execute_action,
    admit_plan_action,
    configure_kernel_activity_ports,
    ensure_goal_delivery,
    list_runtime_actions,
    observe_activity_status,
    observe_carried_activation_activity_status,
    ping_kernel,
)
from orchestration.temporal_workflows import GoalWorkflow
from sqlalchemy import text
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal


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


def test_compose_real_kernel_activities_no_goal_done(api, objects, monkeypatch):
    """compose Server + 真 Kernel Activities；观察终态 ≠ Goal DONE；禁双调度。"""
    target = _resolve_target()
    if not target:
        pytest.skip("本机 Temporal（RING_TEMPORAL_TARGET 或 127.0.0.1:7233）不可连")

    host, _, port_s = target.partition(":")
    if not _temporal_reachable(host, int(port_s or "7233")):
        pytest.skip(f"Temporal target 不可达: {target}")

    http, token, _auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    subject = str(uuid4())
    _register_worker(subject)
    monkeypatch.setenv("RING_WORKFLOW_WORKER_SUBJECT", subject)
    monkeypatch.setenv("RING_TEMPORAL_TARGET", target)
    monkeypatch.setenv("RING_DATABASE_URL", os.environ["RING_TEST_DATABASE_URL"])

    # TEMPORAL Goal 不得计入 LEGACY 开放台账
    legacy_snapshot = count_legacy_goals_with_open_work(engine)
    assert "legacy_goals_open" in legacy_snapshot

    real = RealTemporalClient(target=target)
    configure_kernel_activity_ports(engine=engine, temporal_client=real)

    control_activities = [
        ensure_goal_delivery,
        list_runtime_actions,
        admit_plan_action,
        admit_execute_action,
        observe_activity_status,
        observe_carried_activation_activity_status,
        ping_kernel,
    ]
    plan_id = plan["id"]

    @activity.defn(name="RunActivation")
    async def _run_activation_then_succeed(payload: dict) -> dict:
        """Harness 未接线：诚实 pending；同时把 PLAN 置 SUCCEEDED 供 observe（≠验收）。"""
        assert payload.get("kind") == "PLAN"
        with engine.begin() as db:
            db.execute(
                text(
                    """UPDATE activities SET status='SUCCEEDED', updated_at=clock_timestamp()
                    WHERE id=:id"""
                ),
                {"id": payload["activity_id"]},
            )
        return {
            "status": "NOT_IMPLEMENTED",
            "reason": "Harness RunActivation 未接线（联调 stub）",
            "kind": "PLAN",
            "pending_harness": True,
        }

    async def _run() -> dict:
        temporal = await Client.connect(target)
        with ThreadPoolExecutor(max_workers=8) as pool:
            async with (
                Worker(
                    temporal,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=control_activities,
                    activity_executor=pool,
                ),
                Worker(
                    temporal,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_run_activation_then_succeed],
                ),
            ):
                with engine.connect() as db:
                    row = get_binding_for_goal(db, UUID(goal["id"]))
                assert row is not None
                binding = OrchestrationBindingContent(
                    project_id=row["project_id"],
                    goal_id=row["goal_id"],
                    budget_scope_id=row["budget_scope_id"],
                    backend=row["backend"],
                    owner_epoch=str(row["owner_epoch"]),
                    namespace=row["namespace"],
                    workflow_id=row["workflow_id"],
                    active_run_id=row["active_run_id"],
                    worker_build_id=row["worker_build_id"],
                    contract_digest=row["contract_digest"],
                )
                receipt = await asyncio.to_thread(
                    ensure_workflow,
                    engine,
                    binding,
                    UUID(command["id"]),
                    real,
                    observe_max_ticks=8,
                )
                assert receipt.delivery_status == "ACKNOWLEDGED"

                handle = temporal.get_workflow_handle(binding.workflow_id)
                return await asyncio.wait_for(handle.result(), timeout=90)

    try:
        result = asyncio.run(_run())
        assert result["ok"] is True
        assert result["marks_goal_done"] is False
        assert result.get("pending_harness") is True
        assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
        assert result["plan_terminal_statuses"][0]["activity_id"] == plan_id

        with engine.connect() as db:
            g = (
                db.execute(
                    text("SELECT status, orchestration_backend FROM goals WHERE id=:id"),
                    {"id": goal["id"]},
                )
                .mappings()
                .one()
            )
            delivery = get_delivery_for_command(db, UUID(command["id"]), "ENSURE_WORKFLOW")
        assert g["orchestration_backend"] == "TEMPORAL"
        assert g["status"] != "DONE"
        assert delivery is not None
        assert delivery["delivery_status"] == "ACKNOWLEDGED"

        # 全局 claim 对 TEMPORAL Goal 仍应空租约（即使 PLAN 已终态）
        claimed = http.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={
                "Authorization": "Bearer " + token(subject, ["worker"]),
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["data"]["lease"] is None
    finally:
        configure_kernel_activity_ports(engine=None, temporal_client=None)
        _drain_ready_plans(http, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()
