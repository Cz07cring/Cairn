"""EXECUTE 失租：DISPATCHED→UNKNOWN；对账后原 Activity 离开 RECOVERING；Stop 屏障。

≠ Goal DONE；旧 fencing 永久拒绝；effect 数保持 1。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _register_worker
from test_reconcile import _prepare_dispatched_effect


def test_execute_lease_expire_unknown_then_reconcile_recovers_producer(api, objects):
    (
        client,
        token,
        auth,
        goal,
        worker_auth,
        exec_lease,
        effect,
        artifact,
    ) = _prepare_dispatched_effect(api, objects)
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    old_epoch = exec_lease["lease"]["fencing_epoch"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    assert effect["status"] == "DISPATCHED"

    # 拨过期 → 下一次 claim 触发 expire_stale_leases
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )

    # 触发失租扫描（claim 任意种类即可）
    probe_subject = str(uuid4())
    _register_worker(probe_subject, kinds=("PLAN",))
    probe_auth = {"Authorization": "Bearer " + token(probe_subject, ["worker"])}
    client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**probe_auth, "Idempotency-Key": str(uuid4())},
    )

    unknown = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert unknown["status"] == "UNKNOWN"
    producer = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert producer["status"] == "RECOVERING"

    with engine.begin() as db:
        stop = (
            db.execute(
                text(
                    """SELECT id, status FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .first()
        )
        effect_count = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
        resource = db.execute(
            text("SELECT status FROM resource_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
    assert stop is not None and stop["status"] == "REQUESTED"
    assert effect_count == 1
    assert resource == "QUARANTINED"

    # 旧 fencing 永久拒绝
    heart = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={
            "lease": exec_lease["lease"],
            "renewal_seq": exec_lease["attempt"]["renewal_seq"] + 1,
        },
        headers=worker_auth,
    )
    assert heart.status_code in (409, 400), heart.text

    # 迟到回执：PENDING_RECONCILIATION + EvidenceEnvelope（不盲再 dispatch）
    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": attempt_id,
            "fencing_epoch": old_epoch,
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["data"]["disposition"] == "PENDING_RECONCILIATION"
    still_unknown = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert still_unknown["status"] == "UNKNOWN"

    task_id = exec_lease["activity"]["task_id"]
    evidence = client.get(f"/api/v1/tasks/{task_id}/evidence", headers=auth)
    assert evidence.status_code == 200, evidence.text
    assert len(evidence.json()["data"]) >= 1
    env_id = evidence.json()["data"][0]["id"]

    created = client.post(
        f"/api/v1/effects/{effect['id']}/reconcile",
        json={
            "expected_state_revision": still_unknown["state_revision"],
            "observed_result": "SUCCEEDED",
            "external_ref": "lease-expire-reconcile-1",
            "evidence_ids": [env_id],
            "reason": "失租后核实远端已成功",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 202, created.text
    reconcile_activity_id = created.json()["data"]["result"]["activity_id"]
    assert reconcile_activity_id is not None

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status IN ('READY','RUNNING','RECOVERING')
                  AND kind='RECONCILE' AND id<>:id"""
            ),
            {"id": reconcile_activity_id},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('READY','CANCELLED')"""
            ),
            {"id": reconcile_activity_id},
        )

    subject = str(uuid4())
    _register_worker(subject, kinds=("RECONCILE",))
    rec_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["RECONCILE"], "capabilities": []},
        headers={**rec_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == reconcile_activity_id

    done = client.post(
        f"/internal/v1/activities/{reconcile_activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "effect_id": effect["id"],
                "observed_status": "SUCCEEDED",
                "evidence_ids": [env_id],
            },
        },
        headers=rec_auth,
    )
    assert done.status_code == 200, done.text

    final_effect = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert final_effect["status"] == "SUCCEEDED"
    recovered = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert recovered["status"] == "SUCCEEDED", recovered

    # Stop 屏障：未 CONFIRMED 前资源仍隔离；CONFIRMED 后 RELEASED
    before_stop = client.get(f"/internal/v1/stops/{stop['id']}", headers=auth).json()["data"]
    assert before_stop["status"] == "REQUESTED"
    with engine.begin() as db:
        still_q = db.execute(
            text("SELECT status FROM resource_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
    assert still_q == "QUARANTINED"

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
        headers=worker_auth,
    )
    assert confirmed.status_code == 201, confirmed.text
    final_stop = client.get(f"/internal/v1/stops/{stop['id']}", headers=auth).json()["data"]
    assert final_stop["status"] == "CONFIRMED"
    with engine.begin() as db:
        released = db.execute(
            text("SELECT status FROM resource_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
        effect_count_final = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
    assert released == "RELEASED"
    assert effect_count_final == 1

    goal_row = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_row["status"] != "DONE"

    # cleanup
    client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={
            "expected_state_revision": goal_row["state_revision"],
            "reason": "cleanup",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    _drain_ready_plans(client, token)
    engine.dispose()
