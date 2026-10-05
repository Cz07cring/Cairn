"""AB02：prepare 后 Runner 被杀（失租）→ 恢复复用原 effect_id，零额外 DISPATCH。

PREPARED 从未发出：失租后 Activity 可 READY；Stop 确认前可幂等 prepare 改绑 attempt，
但 dispatch 仍失败关闭。确认后同 effect_id 仅一次 DISPATCHED。≠ Goal DONE。
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from sqlalchemy import create_engine, text
from test_claims import _register_worker
from test_effects import _publish_and_claim_execute


def test_ab02_prepare_then_crash_reuses_same_effect_zero_extra_dispatch(api, objects):
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    blob = b'{"path":"src/ab02.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:ab02",
    )
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "ab02-prepare-crash",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    logical_step_id = step.json()["data"]["logical_step_id"]

    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": logical_step_id,
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect_id = prepared.json()["data"]["id"]
    assert prepared.json()["data"]["status"] == "PREPARED"

    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )
    probe = str(uuid4())
    _register_worker(probe, kinds=("PLAN", "EXECUTE"))
    client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(probe, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )

    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT status FROM effect_intents WHERE id=:id"),
                {"id": effect_id},
            )
            .mappings()
            .one()
        )
        stop = (
            db.execute(
                text(
                    """SELECT id, status FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .one()
        )
        act = db.execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": activity_id},
        ).scalar()
        effect_n = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
    assert row["status"] == "PREPARED"
    assert act == "READY"
    assert stop["status"] == "REQUESTED"
    assert effect_n == 1

    other = str(uuid4())
    _register_worker(other, kinds=("EXECUTE",))
    other_auth = {"Authorization": "Bearer " + token(other, ["worker"])}
    reclaimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**other_auth, "Idempotency-Key": str(uuid4())},
    )
    assert reclaimed.status_code == 200, reclaimed.text
    new_lease = reclaimed.json()["data"]
    assert new_lease["activity"]["id"] == activity_id

    # 幂等续跑：同 logical_step → 同一 effect_id；Stop 未确认前不得 dispatch
    again = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": new_lease["lease"],
            "logical_step_id": logical_step_id,
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=other_auth,
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["id"] == effect_id
    assert again.json()["data"]["status"] == "PREPARED"

    blocked = client.post(
        f"/internal/v1/effects/{effect_id}/dispatch",
        json={
            "lease": new_lease["lease"],
            "effect_state_revision": again.json()["data"]["state_revision"],
        },
        headers=other_auth,
    )
    assert blocked.status_code == 422, blocked.text
    err = blocked.json()["error"]
    assert err["code"] == "STOP_UNCONFIRMED" or "STOP_UNCONFIRMED" in str(err)

    confirm = client.post(
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
    assert confirm.status_code == 201, confirm.text

    # AB09：新 step 的 prepare 在 Stop 确认后才允许；此处续跑原 effect
    # 若 Stop 确认后仍有 QUARANTINED 其它资源？本路径应已 RELEASED
    dispatched = client.post(
        f"/internal/v1/effects/{effect_id}/dispatch",
        json={
            "lease": new_lease["lease"],
            "effect_state_revision": again.json()["data"]["state_revision"],
        },
        headers=other_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["data"]["status"] == "DISPATCHED"

    with engine.begin() as db:
        statuses = list(
            db.execute(
                text(
                    """SELECT status FROM effect_intents
                    WHERE activity_id=:a ORDER BY created_at"""
                ),
                {"a": activity_id},
            ).scalars()
        )
        dispatched_n = db.execute(
            text(
                """SELECT count(*) FROM effect_intents
                WHERE activity_id=:a AND status='DISPATCHED'"""
            ),
            {"a": activity_id},
        ).scalar_one()
        ids = list(
            db.execute(
                text("SELECT id FROM effect_intents WHERE activity_id=:a"),
                {"a": activity_id},
            ).scalars()
        )
    assert statuses == ["DISPATCHED"]
    assert dispatched_n == 1
    assert ids == [UUID(effect_id)]
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"
    engine.dispose()
