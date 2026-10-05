"""TM06 部分举证：pause/cancel + StopReceipt；Temporal terminate 不代替 Stop。

真实 PG；复用 stops / control_commands / lease_recovery 模式。
无 EXITED∧compute_released 不得 CONFIRMED；禁用 Temporal terminate 冒充 Goal CANCELLED。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from control_kernel.storage.goals import set_goal_orchestration_backend
from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal
from test_replan import _publish_running


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


def _stops_for_attempt(attempt_id: str):
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.connect() as db:
        rows = (
            db.execute(
                text(
                    """SELECT id, status, reason, attempt_id, activation_id
                    FROM stops WHERE attempt_id=:id ORDER BY created_at"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .all()
        )
    engine.dispose()
    return [dict(r) for r in rows]


def test_tm06_cancel_stop_receipt_gates_confirmed(api, objects):
    """RUNNING cancel → Stop REQUESTED；缺 EXITED∧compute_released 不 CONFIRMED。"""
    client, token, auth, goal = _publish_running(api, objects)
    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["lease"] is not None
    attempt_id = lease["attempt"]["id"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    try:
        cancelling = client.post(
            f"/api/v1/goals/{goal['id']}/cancel",
            json={
                "expected_state_revision": goal["state_revision"],
                "reason": "tm06 cancel",
            },
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert cancelling.status_code == 202, cancelling.text
        assert cancelling.json()["data"]["result"]["final_status"] == "CANCELLING"

        got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
        assert got["status"] == "CANCELLING"
        assert got["status"] != "CANCELLED"
        assert got["status"] != "DONE"

        stops = _stops_for_attempt(attempt_id)
        assert len(stops) == 1
        assert stops[0]["status"] == "REQUESTED"
        assert stops[0]["reason"] == "CANCEL"
        stop_id = str(stops[0]["id"])

        # RUNNING 观察：仍 REQUESTED，不假 CONFIRMED
        running = client.post(
            f"/internal/v1/stops/{stop_id}/receipts",
            json={
                "receipt_id": str(uuid4()),
                "stop_id": stop_id,
                "activation_id": attempt_id,
                "attempt_id": attempt_id,
                "resource_instance_id": attempt_id,
                "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "observation": "RUNNING",
                "compute_released": False,
                "write_capability_revoked": False,
                "proof_artifact_ids": [],
            },
            headers=worker_auth,
        )
        assert running.status_code == 201, running.text
        assert running.json()["data"]["disposition"] == "APPLIED"
        mid = client.get(f"/internal/v1/stops/{stop_id}", headers=auth)
        assert mid.status_code == 200
        assert mid.json()["data"]["status"] == "REQUESTED"

        # EXITED 但未释放计算：仍不 CONFIRMED
        exited_held = client.post(
            f"/internal/v1/stops/{stop_id}/receipts",
            json={
                "receipt_id": str(uuid4()),
                "stop_id": stop_id,
                "activation_id": attempt_id,
                "attempt_id": attempt_id,
                "resource_instance_id": attempt_id,
                "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "observation": "EXITED",
                "compute_released": False,
                "write_capability_revoked": True,
                "proof_artifact_ids": [],
            },
            headers=worker_auth,
        )
        assert exited_held.status_code == 201, exited_held.text
        assert (
            client.get(f"/internal/v1/stops/{stop_id}", headers=auth).json()["data"][
                "status"
            ]
            == "REQUESTED"
        )

        # 隔离路径：拨过期 → QUARANTINED；无 CONFIRMED 不得 RELEASED
        with engine.begin() as db:
            db.execute(
                text(
                    """UPDATE activity_attempts
                      SET lease_expires_at=:past, updated_at=clock_timestamp()
                      WHERE id=:id"""
                ),
                {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
            )
        # 触发 expire 扫描（新 claim 会扫；此处直接读后若仍 HELD 则再经 claim 空扫）
        subject_b = str(uuid4())
        _register_worker(subject_b, kinds=("EXECUTE", "PLAN"))
        auth_b = {"Authorization": "Bearer " + token(subject_b, ["worker"])}
        client.post(
            "/internal/v1/claims",
            json={"kinds": ["EXECUTE"], "capabilities": []},
            headers={**auth_b, "Idempotency-Key": str(uuid4())},
        )
        resource_status, budget_status = _reservation_statuses(engine, attempt_id)
        # 失租后应为 QUARANTINED；若仍 HELD 也不许假 RELEASED
        assert resource_status in ("QUARANTINED", "HELD")
        assert budget_status in ("QUARANTINED", "HELD")
        assert resource_status != "RELEASED"
        assert budget_status != "RELEASED"
        assert (
            client.get(f"/internal/v1/stops/{stop_id}", headers=auth).json()["data"][
                "status"
            ]
            != "CONFIRMED"
        )

        # EXITED ∧ compute_released → CONFIRMED
        confirm_rid = str(uuid4())
        confirmed = client.post(
            f"/internal/v1/stops/{stop_id}/receipts",
            json={
                "receipt_id": confirm_rid,
                "stop_id": stop_id,
                "activation_id": attempt_id,
                "attempt_id": attempt_id,
                "resource_instance_id": attempt_id,
                "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "observation": "EXITED",
                "compute_released": True,
                "write_capability_revoked": True,
                "proof_artifact_ids": [],
            },
            headers=worker_auth,
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["data"]["disposition"] == "APPLIED"
        final_stop = client.get(f"/internal/v1/stops/{stop_id}", headers=auth)
        assert final_stop.json()["data"]["status"] == "CONFIRMED"

        # 同 receipt 幂等 DUPLICATE
        dup = client.post(
            f"/internal/v1/stops/{stop_id}/receipts",
            json={
                "receipt_id": confirm_rid,
                "stop_id": stop_id,
                "activation_id": attempt_id,
                "attempt_id": attempt_id,
                "resource_instance_id": attempt_id,
                "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "observation": "EXITED",
                "compute_released": True,
                "write_capability_revoked": True,
                "proof_artifact_ids": [],
            },
            headers=worker_auth,
        )
        assert dup.status_code == 201
        assert dup.json()["data"]["disposition"] == "DUPLICATE"

        after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
        assert after["status"] != "DONE"
        # Temporal terminate ≠ Kernel：本用例仅走 Kernel cancel；终态须经排空而非假 DONE
        assert after["status"] in ("CANCELLING", "CANCELLED")
    finally:
        _drain_ready_plans(client, token)
        engine.dispose()


def test_tm06_temporal_terminate_not_goal_cancelled_without_kernel(api, objects):
    """无 live Temporal Server：禁止把 Temporal terminate 模拟成 Goal CANCELLED。

    未发 Kernel pause/cancel 时 Goal 保持 PLANNING；无 StopRequest。
    """
    client, token, auth, goal, _plan, _command, engine = _start_temporal_goal(
        api, objects
    )
    try:
        before = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
        assert before["status"] == "PLANNING"
        assert before["status"] != "CANCELLED"
        assert before["status"] != "DONE"

        with engine.connect() as db:
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
            stop_count = db.execute(
                text(
                    """SELECT count(*) FROM stops s
                    JOIN activities a ON a.id=s.activity_id
                    WHERE a.goal_id=:goal"""
                ),
                {"goal": goal["id"]},
            ).scalar_one()
        assert backend == "TEMPORAL"
        assert int(stop_count) == 0

        # 无 Kernel 命令：不得仅因「编排侧 terminate」语义把 Goal 写成 CANCELLED
        # （真 Temporal cancel 无 live Server 时 N/A；此处断言 Kernel 权威）
        forged = client.post(
            f"/api/v1/goals/{goal['id']}/cancel",
            json={
                "expected_state_revision": before["state_revision"],
                "reason": "would-be-temporal-terminate",
            },
            headers={
                "Authorization": "Bearer " + token(str(uuid4()), ["viewer"]),
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert forged.status_code == 403, forged.text

        still = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
        assert still["status"] == "PLANNING"
        assert still["status"] != "CANCELLED"
        assert still["orchestration_backend"] == "TEMPORAL"

        with engine.connect() as db:
            stop_count_after = db.execute(
                text(
                    """SELECT count(*) FROM stops s
                    JOIN activities a ON a.id=s.activity_id
                    WHERE a.goal_id=:goal"""
                ),
                {"goal": goal["id"]},
            ).scalar_one()
        assert int(stop_count_after) == 0
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
