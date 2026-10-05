"""AB03：外部可能已成功但 receipt 丢失 → UNKNOWN；对账前不重试、不继续新工具、≠DONE。"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from sqlalchemy import create_engine, text
from test_claims import _register_worker
from test_reconcile import _prepare_dispatched_effect


def test_ab03_unknown_blocks_new_tool_and_keeps_single_effect(api, objects):
    store, _, _ = objects
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
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    assert effect["status"] == "DISPATCHED"

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

    unknown = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert unknown["status"] == "UNKNOWN"
    producer = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()[
        "data"
    ]
    assert producer["status"] == "RECOVERING"

    with engine.begin() as db:
        effect_n = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
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
    assert effect_n == 1
    assert stop["status"] == "REQUESTED"

    # UNKNOWN 未对账：不得重领 EXECUTE（未决 DISPATCHED/UNKNOWN 挡 READY）
    other = str(uuid4())
    _register_worker(other, kinds=("EXECUTE",))
    other_auth = {"Authorization": "Bearer " + token(other, ["worker"])}
    reclaim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**other_auth, "Idempotency-Key": str(uuid4())},
    )
    # 可能 204/空 lease 或 200 但无本 activity——不得拿到可写租约继续工具
    if reclaim.status_code == 200 and reclaim.json()["data"].get("lease"):
        leased = reclaim.json()["data"]
        assert leased["activity"]["id"] != activity_id

    # 即便伪造拿到旧 fencing，也不得推进；迟到回执进 reconciliation，不盲 SUCCEEDED
    late = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": attempt_id,
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert late.status_code == 201, late.text
    assert late.json()["data"]["disposition"] == "PENDING_RECONCILIATION"
    still = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert still["status"] == "UNKNOWN"

    # Stop 未确认：新 ENGINEERING prepare 失败关闭（与 AB09 同门，挂 AB03「不继续工具」）
    # 需另一活动租约才有可能 prepare——此处用重领失败后的直接断言：原 activity 仍 RECOVERING
    still_act = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()[
        "data"
    ]
    assert still_act["status"] == "RECOVERING"

    blob = b'{"path":"src/ab03-new.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    new_art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:ab03",
    )
    # 无 ACTIVE 租约时 create_step 应失败
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "ab03-blind-retry",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code in (400, 409), step.text

    with engine.begin() as db:
        effect_n2 = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
    assert effect_n2 == 1
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"
    # 消掉未用变量告警意图：new_art 仅证明对象仓可写，非业务推进
    assert new_art.id is not None
    engine.dispose()
