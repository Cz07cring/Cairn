"""失租：过期 attempt → EXPIRED + 资源 QUARANTINED；活动可重领，资源须停机确认才释放。

裁定（hermes-c04，2026-09-12）：重领与资源释放是两件事。
- 重领：`RECOVERING→READY` 只需「effect 可安全续跑」（doc/01 状态机）——即无未决 effect。
- 释放：资源/预算保持 QUARANTINED，直到收到 `EXITED ∧ compute_released` 可信回执
  （v0.6/01：旧进程未知时资源保留隔离、费用待核对；禁止失租即释放）。
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _register_worker, _start_goal


def _reservation_statuses(engine, attempt_id):
    with engine.begin() as db:
        resource = db.execute(
            text("SELECT status FROM resource_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
        budget = db.execute(
            text("SELECT status FROM budget_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
    return resource, budget


def _force_expire(engine, attempt_id):
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )


def test_expired_lease_awaits_stop_before_new_claim(api, objects):
    client, token, auth, goal, _plan = _start_goal(api, objects)
    subject_a = str(uuid4())
    _register_worker(subject_a)
    auth_a = {"Authorization": "Bearer " + token(subject_a, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**auth_a, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["lease"] is not None
    activity_id = lease["activity"]["id"]
    attempt_id = lease["lease"]["attempt_id"]
    epoch_a = lease["lease"]["fencing_epoch"]

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    _force_expire(engine, attempt_id)

    # 旧心跳必须失败（触发 expire 扫描）
    heart = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={
            "lease": lease["lease"],
            "renewal_seq": lease["attempt"]["renewal_seq"] + 1,
        },
        headers=auth_a,
    )
    assert heart.status_code in (409, 400), heart.text

    # 失租后：无未决 effect 即可重领（doc/01：RECOVERING→READY 条件为「effect 可安全续跑」）。
    # 但资源/预算保持 QUARANTINED，直到可信停机回执——「等确认」的是资源释放，不是活动重领。
    subject_b = str(uuid4())
    _register_worker(subject_b)
    auth_b = {"Authorization": "Bearer " + token(subject_b, ["worker"])}
    reclaim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**auth_b, "Idempotency-Key": str(uuid4())},
    )
    assert reclaim.status_code == 200, reclaim.text
    lease_b = reclaim.json()["data"]
    assert lease_b["lease"] is not None
    assert lease_b["activity"]["id"] == activity_id
    assert int(lease_b["lease"]["fencing_epoch"]) > int(epoch_a)

    activity = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert activity["status"] == "RUNNING"
    assert activity["current_attempt_id"] == lease_b["lease"]["attempt_id"]

    with engine.begin() as db:
        stop = (
            db.execute(
                text(
                    """SELECT id, reason, status, activation_id, attempt_id FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .first()
        )
    assert stop is not None
    assert stop["status"] == "REQUESTED"
    assert str(stop["activation_id"]) == attempt_id
    assert str(stop["attempt_id"]) == attempt_id

    resource_status, budget_status = _reservation_statuses(engine, attempt_id)
    assert resource_status == "QUARANTINED"
    assert budget_status == "QUARANTINED"

    status = client.get(
        "/api/v1/system/status",
        params={"project_id": goal["project_id"]},
        headers=auth,
    )
    assert status.status_code == 200, status.text
    assert status.json()["data"]["resources"]["quarantined_resources"] >= 1

    # ISOLATED + compute_released=false：不得释放、不得 READY
    isolated = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop["id"]),
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "ISOLATED",
            "compute_released": False,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=auth_a,
    )
    assert isolated.status_code == 201, isolated.text
    assert isolated.json()["data"]["disposition"] == "APPLIED"
    still = client.get(f"/internal/v1/stops/{stop['id']}", headers=auth)
    assert still.json()["data"]["status"] == "REQUESTED"
    still_act = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    # ISOLATED 不是停机确认：新 attempt 继续持有活动，资源保持隔离
    assert still_act["status"] == "RUNNING"
    resource_status, budget_status = _reservation_statuses(engine, attempt_id)
    assert resource_status == "QUARANTINED"
    assert budget_status == "QUARANTINED"

    # EXITED ∧ compute_released → CONFIRMED + RELEASED + READY
    confirmed = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop["id"]),
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=auth_a,
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["data"]["disposition"] == "APPLIED"
    final = client.get(f"/internal/v1/stops/{stop['id']}", headers=auth)
    assert final.json()["data"]["status"] == "CONFIRMED"
    resource_status, budget_status = _reservation_statuses(engine, attempt_id)
    assert resource_status == "RELEASED"
    assert budget_status == "RELEASED"

    # 停机确认只释放旧 attempt 的资源；活动已由新 attempt 持有，不被本回执改状态
    ready_act = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert ready_act["status"] == "RUNNING"

    # 旧 epoch 不可再写 heartbeat
    heart2 = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={
            "lease": lease["lease"],
            "renewal_seq": lease["attempt"]["renewal_seq"] + 1,
        },
        headers=auth_a,
    )
    assert heart2.status_code in (409, 400), heart2.text

    activity = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert activity["status"] == "RUNNING"
    assert activity["current_attempt_id"] == lease_b["lease"]["attempt_id"]
    engine.dispose()


def test_expired_with_terminal_effect_still_awaits_stop(api, objects):
    """终态 effect（SUCCEEDED）不算未决，故可重领；资源仍须停机确认才释放。"""
    import hashlib
    from io import BytesIO
    from uuid import UUID

    from control_kernel.storage.artifacts import Artifacts

    client, token, auth, goal, _plan = _start_goal(api, objects)
    store, _, _ = objects
    subject = str(uuid4())
    _register_worker(subject)
    auth_w = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**auth_w, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    activity_id = lease["activity"]["id"]
    attempt_id = lease["lease"]["attempt_id"]
    project_id = UUID(goal["project_id"])

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    blob = b'{"path":"x"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{subject}",
    )
    step_id = uuid4()
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO effect_intents(
                  id, project_id, goal_id, activity_id, logical_step_id, intent_revision,
                  payload_digest, tool_ref, replay_class, scope, status, state_revision,
                  evidence_ids, input_artifact_id, producer_attempt_id)
                VALUES (
                  :id, :project, :goal, :activity, :step, 1,
                  :digest, 'read_file', 'READ_ONLY', 'ENGINEERING', 'SUCCEEDED', 1,
                  '{}', :artifact, :attempt)"""
            ),
            {
                "id": step_id,
                "project": project_id,
                "goal": UUID(goal["id"]),
                "activity": UUID(activity_id),
                "step": step_id,
                "digest": digest,
                "artifact": artifact.id,
                "attempt": UUID(attempt_id),
            },
        )

    _force_expire(engine, attempt_id)
    heart = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={
            "lease": lease["lease"],
            "renewal_seq": lease["attempt"]["renewal_seq"] + 1,
        },
        headers=auth_w,
    )
    assert heart.status_code in (409, 400), heart.text

    # claim 触发 expire_stale_leases：终态 effect 不属未决，故可重领
    other = str(uuid4())
    _register_worker(other)
    reclaimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(other, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert reclaimed.status_code == 200, reclaimed.text
    lease_new = reclaimed.json()["data"]
    assert lease_new["lease"] is not None
    assert int(lease_new["lease"]["fencing_epoch"]) > int(lease["lease"]["fencing_epoch"])

    # 重领不影响旧 attempt 的资源隔离：仍 QUARANTINED，待停机回执确认后释放
    act = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert act["status"] == "RUNNING"

    with engine.begin() as db:
        stop_id = db.execute(
            text(
                """SELECT id FROM stops
                WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
            ),
            {"id": attempt_id},
        ).scalar_one()

    confirmed = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop_id),
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=auth_w,
    )
    assert confirmed.status_code == 201, confirmed.text
    # 停机已确认 → 旧 attempt 的资源释放；活动已由新 attempt 持有，不被本回执改状态
    resource_status, budget_status = _reservation_statuses(engine, attempt_id)
    assert resource_status == "RELEASED"
    assert budget_status == "RELEASED"
    ready = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert ready["status"] == "RUNNING"
    engine.dispose()
