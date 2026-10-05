"""TM02 部分举证：同一 Kernel 命令重投 → 同结果、不重复副作用。

真实 PG + Fake Temporal（可选 relay）；禁 LEGACY fallback。
抽样 20 次同 Idempotency-Key（全量 100 可升）；非真 Temporal Server。
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import get_delivery_for_command
from orchestration import FakeTemporalClient, OrchestrationBindingContent, ensure_workflow
from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _ready_project

# TM02 规范为 100 次；CI 抽样 20（全量 100 可升）
_TM02_REPLAY_SAMPLE = 20


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


def test_tm02_start_same_idempotency_key_no_duplicate_side_effects(api, objects):
    """同 Idempotency-Key 重投 START：同 command id；一 PENDING/ACK delivery、一 binding、一 PLAN。"""
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    _drain_ready_plans(client, token)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1")

        idem_key = str(uuid4())
        body = {"expected_state_revision": 1, "reason": "tm02-plan"}
        commands: list[dict] = []
        for _ in range(_TM02_REPLAY_SAMPLE):  # 全量 100 可升
            started = client.post(
                f"/api/v1/goals/{goal['id']}/start",
                json=body,
                headers={**auth, "Idempotency-Key": idem_key},
            )
            assert started.status_code == 202, started.text
            commands.append(started.json()["data"])

        first = commands[0]
        assert first["kind"] == "START"
        assert first["status"] == "ACCEPTED"
        assert first["result"]["final_status"] == "PLANNING"
        assert all(c == first for c in commands)
        assert len({c["id"] for c in commands}) == 1

        with engine.connect() as db:
            bindings = db.execute(
                text("SELECT count(*) FROM orchestration_bindings WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).scalar_one()
            plans = db.execute(
                text(
                    """SELECT count(*) FROM activities
                    WHERE goal_id=:goal AND kind='PLAN'"""
                ),
                {"goal": goal["id"]},
            ).scalar_one()
            deliveries = db.execute(
                text(
                    """SELECT count(*) FROM orchestration_deliveries
                    WHERE command_id=:cmd"""
                ),
                {"cmd": first["id"]},
            ).scalar_one()
            delivery = get_delivery_for_command(db, UUID(first["id"]))
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()

        assert bindings == 1
        assert plans == 1
        assert deliveries == 1
        assert delivery is not None
        assert delivery["delivery_status"] == "PENDING"
        assert backend == "TEMPORAL"
        assert goal_status == "PLANNING"
        assert goal_status != "DONE"

        # START + project_requests 已幂等；relay ACK 亦不增 delivery/binding/PLAN
        with engine.connect() as db:
            row = (
                db.execute(
                    text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                    {"goal": goal["id"]},
                )
                .mappings()
                .one()
            )
        binding = _binding_content(row)
        fake = FakeTemporalClient()
        receipt = ensure_workflow(engine, binding, UUID(first["id"]), fake)
        assert receipt.delivery_status == "ACKNOWLEDGED"
        again = ensure_workflow(engine, binding, UUID(first["id"]), fake)
        assert again.run_id == receipt.run_id
        assert again.model_dump() == receipt.model_dump()

        # 再重投 START：仍同 command，副作用计数不变
        for _ in range(3):
            replay = client.post(
                f"/api/v1/goals/{goal['id']}/start",
                json=body,
                headers={**auth, "Idempotency-Key": idem_key},
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["data"] == first

        with engine.connect() as db:
            assert (
                db.execute(
                    text(
                        """SELECT count(*) FROM orchestration_deliveries
                        WHERE command_id=:cmd"""
                    ),
                    {"cmd": first["id"]},
                ).scalar_one()
                == 1
            )
            assert (
                get_delivery_for_command(db, UUID(first["id"]))["delivery_status"]
                == "ACKNOWLEDGED"
            )
            assert (
                db.execute(
                    text("SELECT count(*) FROM orchestration_bindings WHERE goal_id=:goal"),
                    {"goal": goal["id"]},
                ).scalar_one()
                == 1
            )
            assert (
                db.execute(
                    text(
                        """SELECT count(*) FROM activities
                        WHERE goal_id=:goal AND kind='PLAN'"""
                    ),
                    {"goal": goal["id"]},
                ).scalar_one()
                == 1
            )
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
