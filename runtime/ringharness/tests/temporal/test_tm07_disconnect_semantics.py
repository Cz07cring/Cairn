"""TM07 部分举证：Temporal 与业务 PG 分别断连语义。

真实 PG + Fake Temporal Unavailable；禁 LEGACY fallback、禁假 SUCCEEDED 编排。
命令保持 ACCEPTED + PENDING；Goal API 仍 PLANNING（非撒谎 running/DONE）。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import get_delivery_for_command
from orchestration import (
    FakeTemporalClient,
    OrchestrationBindingContent,
    TemporalUnavailable,
    ensure_workflow,
)
from sqlalchemy import text
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal


def _binding_content(row) -> OrchestrationBindingContent:
    return OrchestrationBindingContent(
        project_id=row["project_id"],
        goal_id=row["goal_id"],
        budget_scope_id=row["budget_scope_id"],
        backend=row["backend"],
        owner_epoch=row["owner_epoch"],
        namespace=row["namespace"],
        workflow_id=row["workflow_id"],
        active_run_id=row["active_run_id"],
        worker_build_id=row["worker_build_id"],
        contract_digest=row["contract_digest"],
    )


def _assert_claim_empty(client, token) -> None:
    subject = str(uuid4())
    _register_worker(subject)
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(subject, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["lease"] is None


def test_tm07_temporal_unavailable_keeps_accepted_pending_planning(api, objects):
    """Temporal Unavailable：START 仍 ACCEPTED+PENDING；backend TEMPORAL；Goal 仍 PLANNING。"""
    client, token, auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        assert command["status"] == "ACCEPTED"
        assert command["status"] != "SUCCEEDED"
        assert command["result"]["final_status"] == "PLANNING"

        with engine.connect() as db:
            binding_row = (
                db.execute(
                    text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                    {"goal": goal["id"]},
                )
                .mappings()
                .one()
            )
            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery["delivery_status"] == "PENDING"
            assert delivery["run_id"] is None

        fake = FakeTemporalClient()
        fake.unavailable = True
        try:
            ensure_workflow(
                engine, _binding_content(binding_row), UUID(command["id"]), fake
            )
            raise AssertionError("expected TemporalUnavailable")
        except TemporalUnavailable:
            pass

        with engine.connect() as db:
            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery["delivery_status"] == "PENDING"
            assert delivery["run_id"] is None
            cmd_status = db.execute(
                text("SELECT status FROM command_operations WHERE id=:id"),
                {"id": command["id"]},
            ).scalar_one()
            assert cmd_status == "ACCEPTED"
            assert cmd_status != "SUCCEEDED"
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
            assert backend == "TEMPORAL"
            goal_row = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
            assert goal_row == "PLANNING"
            assert goal_row != "DONE"

        # UI/API：Goal 仍可见为 PLANNING，不撒谎 running/DONE
        got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
        assert got.status_code == 200, got.text
        data = got.json()["data"]
        assert data["status"] == "PLANNING"
        assert data["status"] != "DONE"
        assert data.get("orchestration_backend", "TEMPORAL") == "TEMPORAL"

        cmds = client.get(
            "/api/v1/commands",
            params={"goal_id": goal["id"]},
            headers=auth,
        )
        assert cmds.status_code == 200, cmds.text
        start = next(c for c in cmds.json()["data"] if c["id"] == command["id"])
        assert start["status"] == "ACCEPTED"
        assert start["kind"] == "START"

        _assert_claim_empty(client, token)
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()


def test_tm07_relay_fail_after_pending_leaves_pending(api, objects):
    """可选：已 PENDING 后 client Unavailable → relay 失败仍 PENDING，不假成功。"""
    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        with engine.connect() as db:
            binding_row = (
                db.execute(
                    text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                    {"goal": goal["id"]},
                )
                .mappings()
                .one()
            )
            assert get_delivery_for_command(db, UUID(command["id"]))["delivery_status"] == "PENDING"

        fake = FakeTemporalClient()
        fake.unavailable = True
        try:
            ensure_workflow(
                engine, _binding_content(binding_row), UUID(command["id"]), fake
            )
            raise AssertionError("expected TemporalUnavailable")
        except TemporalUnavailable:
            pass

        with engine.connect() as db:
            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery["delivery_status"] == "PENDING"
            assert (
                db.execute(
                    text("SELECT status FROM command_operations WHERE id=:id"),
                    {"id": command["id"]},
                ).scalar_one()
                == "ACCEPTED"
            )
            assert (
                db.execute(
                    text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                    {"id": goal["id"]},
                ).scalar_one()
                == "TEMPORAL"
            )
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
