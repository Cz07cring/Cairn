"""UNKNOWN effect 对账：reconcile 创建 RECONCILE，outcome 推进 SUCCEEDED。"""

import hashlib
import os
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _register_worker
from test_effects import _publish_and_claim_execute


def _prepare_dispatched_effect(api, objects):
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "读取入口",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]

    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{exec_lease['attempt']['worker_id']}",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    return (
        client,
        token,
        auth,
        goal,
        worker_auth,
        exec_lease,
        dispatched.json()["data"],
        artifact,
    )


def test_reconcile_unknown_effect_and_lists(api, objects):
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

    steps = client.get(f"/api/v1/activities/{activity_id}/steps", headers=auth)
    assert steps.status_code == 200, steps.text
    assert len(steps.json()["data"]) >= 1
    assert steps.json()["data"][0]["tool_ref"] == "read_file"

    attempts = client.get(f"/api/v1/activities/{activity_id}/attempts", headers=auth)
    assert attempts.status_code == 200, attempts.text
    assert any(row["id"] == exec_lease["attempt"]["id"] for row in attempts.json()["data"])

    # 活租约 + UNKNOWN 回执 → effect UNKNOWN，并生成 EvidenceEnvelope
    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": exec_lease["lease"]["attempt_id"],
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": None,
            "timed_out": True,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "UNKNOWN",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["data"]["disposition"] == "APPLIED"
    unknown = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert unknown["status"] == "UNKNOWN"

    task_id = exec_lease["activity"]["task_id"]
    evidence = client.get(f"/api/v1/tasks/{task_id}/evidence", headers=auth)
    assert evidence.status_code == 200, evidence.text
    env_id = evidence.json()["data"][0]["id"]

    idem = str(uuid4())
    body = {
        "expected_state_revision": unknown["state_revision"],
        "observed_result": "SUCCEEDED",
        "external_ref": "manual-reconcile-1",
        "evidence_ids": [env_id],
        "reason": "超时后核实远端已成功",
    }
    created = client.post(
        f"/api/v1/effects/{effect['id']}/reconcile",
        json=body,
        headers={**auth, "Idempotency-Key": idem},
    )
    assert created.status_code == 202, created.text
    command = created.json()["data"]
    assert command["kind"] == "RECONCILE_EFFECT"
    assert command["result"]["activity_id"] is not None
    assert command["result"]["effect_id"] == effect["id"]
    reconcile_activity_id = command["result"]["activity_id"]

    # 命令路径不直接改 effect
    still = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert still["status"] == "UNKNOWN"

    # 幂等重传
    replay = client.post(
        f"/api/v1/effects/{effect['id']}/reconcile",
        json=body,
        headers={**auth, "Idempotency-Key": idem},
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["id"] == command["id"]

    # 只保留本 RECONCILE，避免共享库串扰
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
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
    engine.dispose()

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
    assert lease["activity"]["kind"] == "RECONCILE"

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
    assert done.json()["data"]["status"] == "SUCCEEDED"

    final = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert final["status"] == "SUCCEEDED"

    # 已非 UNKNOWN，新 key 拒绝
    again = client.post(
        f"/api/v1/effects/{effect['id']}/reconcile",
        json={
            **body,
            "expected_state_revision": final["state_revision"],
            "external_ref": "manual-reconcile-2",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert again.status_code == 422

    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={"expected_state_revision": got["state_revision"], "reason": "cleanup"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    _drain_ready_plans(client, token)


def test_reconcile_requires_approver(api, objects):
    client, token = api
    subject = str(uuid4())
    auth = {"Authorization": "Bearer " + token(subject, ["operator", "viewer"])}
    denied = client.post(
        f"/api/v1/effects/{uuid4()}/reconcile",
        json={
            "expected_state_revision": 1,
            "observed_result": "SUCCEEDED",
            "external_ref": "x",
            "evidence_ids": [str(uuid4())],
            "reason": "无权",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"


def test_reconcile_clears_unknown_promotes_draining_barrier_to_sealed(api, objects):
    """UNKNOWN 卡住 DRAINING；对账 SUCCEEDED 后 try_seal → SEALED+FINALIZE；≠DONE。"""
    import json

    from control_kernel.storage.finalization import open_finalization_barrier
    from test_ab09_stop_barrier import _ensure_global_criterion

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
    goal_id = goal["id"]
    project_id = exec_lease["activity"]["project_id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    task_id = exec_lease["activity"]["task_id"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": attempt_id,
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": None,
            "timed_out": True,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "UNKNOWN",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    unknown = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert unknown["status"] == "UNKNOWN"

    _ensure_global_criterion(client, auth, engine, project_id, goal_id)

    candidate_id = uuid4()
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO candidate_manifests(
                  id,project_id,goal_id,task_id,activity_id,attempt_id,
                  protected_baseline_digest,content_digest,content,
                  workspace_snapshot_artifact_id)
                VALUES(
                  :id,:project,:goal,:task,:activity,:attempt,
                  :baseline,:digest,CAST(:content AS jsonb),:snap)"""
            ),
            {
                "id": candidate_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "activity": activity_id,
                "attempt": attempt_id,
                "baseline": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "content": json.dumps({"files": []}),
                "snap": artifact.id,
            },
        )
        open_finalization_barrier(db, UUID(goal_id), candidate_id)
        barrier = (
            db.execute(
                text(
                    """SELECT status, unknown_effects FROM finalization_barriers
                    WHERE goal_id=:goal"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        assert barrier["status"] == "DRAINING"
        assert int(barrier["unknown_effects"]) >= 1

    evidence = client.get(f"/api/v1/tasks/{task_id}/evidence", headers=auth)
    assert evidence.status_code == 200, evidence.text
    env_id = evidence.json()["data"][0]["id"]

    created = client.post(
        f"/api/v1/effects/{effect['id']}/reconcile",
        json={
            "expected_state_revision": unknown["state_revision"],
            "observed_result": "SUCCEEDED",
            "external_ref": "ab09-reconcile-seal-1",
            "evidence_ids": [env_id],
            "reason": "对账清除 UNKNOWN 以晋升屏障",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 202, created.text
    reconcile_activity_id = created.json()["data"]["result"]["activity_id"]

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
    engine.dispose()

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

    final = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert final["status"] == "SUCCEEDED"

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "VERIFYING"
    assert got["status"] != "DONE"
    assert got["barrier"]["status"] == "SEALED"

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.connect() as db:
        finalize_n = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND kind='FINALIZE' AND status='READY'"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        assert int(finalize_n) == 1
    eng.dispose()
