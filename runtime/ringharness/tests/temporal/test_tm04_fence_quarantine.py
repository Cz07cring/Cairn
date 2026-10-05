"""TM04 部分举证：Worker 失联旧进程仍活 → 旧 fence 拒绝、资源隔离、不可提前 DONE。

真实 PG；复用 lease_recovery 失租→QUARANTINED 模式，经 TEMPORAL admit 路径。
禁把旧租约心跳/outcome 写成成功；Goal 不得 DONE。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from control_kernel.storage.goals import set_goal_orchestration_backend
from orchestration import FakeTemporalClient, OrchestrationBindingContent, ensure_workflow
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


def _reservation_statuses(engine, attempt_id):
    with engine.connect() as db:
        resource = db.execute(
            text("SELECT status FROM resource_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
        budget = db.execute(
            text("SELECT status FROM budget_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
    return resource, budget


def test_tm04_expired_admit_quarantines_and_rejects_old_fence(api, objects):
    """TEMPORAL admit PLAN → 失租 QUARANTINED；新 admit 新 fencing；旧 fence/outcome 拒绝；Goal≠DONE。"""
    client, token, auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
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
        ensure_workflow(
            engine,
            _binding_content(binding_row),
            UUID(command["id"]),
            FakeTemporalClient(),
        )

        subject_a = str(uuid4())
        _register_worker(subject_a)
        auth_a = {"Authorization": "Bearer " + token(subject_a, ["worker"])}
        admitted = client.post(
            "/internal/v1/runtime/admit",
            json={"activity_id": plan["id"]},
            headers={**auth_a, "Idempotency-Key": str(uuid4())},
        )
        assert admitted.status_code == 200, admitted.text
        lease_a = admitted.json()["data"]
        assert lease_a["lease"] is not None
        activity_id = lease_a["activity"]["id"]
        attempt_id = lease_a["lease"]["attempt_id"]
        epoch_a = lease_a["lease"]["fencing_epoch"]

        # 模拟失租：拨过期时间（不碰业务 state_revision）
        with engine.begin() as db:
            db.execute(
                text(
                    """UPDATE activity_attempts
                      SET lease_expires_at=:past, updated_at=clock_timestamp()
                      WHERE id=:id"""
                ),
                {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
            )

        # 旧心跳必须失败（租约已过期，不可复活）
        heart = client.post(
            f"/internal/v1/activities/{activity_id}/heartbeat",
            json={
                "lease": lease_a["lease"],
                "renewal_seq": lease_a["attempt"]["renewal_seq"] + 1,
            },
            headers=auth_a,
        )
        assert heart.status_code in (409, 400), heart.text
        assert heart.json()["error"]["code"] in ("LEASE_EXPIRED", "INVALID_STATE", "FENCING_REJECTED")

        # 新 worker admit：触发 expire 扫描 → 新 fencing
        subject_b = str(uuid4())
        _register_worker(subject_b)
        auth_b = {"Authorization": "Bearer " + token(subject_b, ["worker"])}
        recovered = client.post(
            "/internal/v1/runtime/admit",
            json={"activity_id": plan["id"]},
            headers={**auth_b, "Idempotency-Key": str(uuid4())},
        )
        assert recovered.status_code == 200, recovered.text
        lease_b = recovered.json()["data"]
        assert lease_b["lease"] is not None
        assert lease_b["activity"]["id"] == activity_id
        assert lease_b["lease"]["fencing_epoch"] != epoch_a
        assert int(lease_b["lease"]["fencing_epoch"]) > int(epoch_a)

        resource_status, budget_status = _reservation_statuses(engine, attempt_id)
        assert resource_status == "QUARANTINED"
        assert budget_status == "QUARANTINED"

        # 旧 attempt 心跳仍拒绝
        heart_old = client.post(
            f"/internal/v1/activities/{activity_id}/heartbeat",
            json={
                "lease": lease_a["lease"],
                "renewal_seq": lease_a["attempt"]["renewal_seq"] + 1,
            },
            headers=auth_a,
        )
        assert heart_old.status_code in (409, 400), heart_old.text

        # 显式旧 fence：新 attempt + 旧 fencing_epoch → FENCING_REJECTED
        heart_fence = client.post(
            f"/internal/v1/activities/{activity_id}/heartbeat",
            json={
                "lease": {**lease_b["lease"], "fencing_epoch": epoch_a},
                "renewal_seq": lease_b["attempt"]["renewal_seq"] + 1,
            },
            headers=auth_b,
        )
        assert heart_fence.status_code in (409, 400), heart_fence.text
        assert heart_fence.json()["error"]["code"] == "FENCING_REJECTED"

        # 旧租约 outcome 拒绝（不可提前写终态 / DONE）
        outcome = client.post(
            f"/internal/v1/activities/{activity_id}/outcomes",
            json={
                "lease": lease_a["lease"],
                "expected_state_revision": lease_b["activity"]["state_revision"],
                "outcome": {"plan": {"tasks": [], "coverage": [], "edges": []}},
            },
            headers=auth_a,
        )
        assert outcome.status_code in (409, 400, 422), outcome.text
        err = outcome.json()["error"]["code"]
        assert err in ("FENCING_REJECTED", "INVALID_STATE", "LEASE_EXPIRED", "VALIDATION_ERROR")

        got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
        assert got.status_code == 200, got.text
        assert got.json()["data"]["status"] == "PLANNING"
        assert got.json()["data"]["status"] != "DONE"

        with engine.connect() as db:
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
        assert backend == "TEMPORAL"
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
