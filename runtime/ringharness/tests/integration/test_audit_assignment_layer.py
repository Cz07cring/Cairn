"""AUDIT assignment.layer 必须来自 VerificationProfile.required_layers。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from evidence_ledger.content import encode
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker, _start_goal
from test_plans import _plan_body


def _seed_task_layer_profile(client, auth, project_id: str, *, layer: str) -> str:
    examples = json.loads(
        (Path(__file__).parents[2] / "doc/contracts/verifier-fixtures-v2.json").read_text()
    )["examples"]
    example = next(ex for ex in examples if ex["layer"] == layer)
    definition = {**example["definition"], "project_id": project_id}
    digest = (
        "sha256:"
        + hashlib.sha256(
            encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": "VerifierDefinition",
                        "content": definition,
                        "reference_bindings": [],
                    }
                )
            )
        ).hexdigest()
    )
    ref = f"approved.layer.{layer.lower()}.{uuid4().hex[:8]}"
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""INSERT INTO verifier_definitions(
              project_id,ref,content_digest,content,approval_record_digest)
            VALUES(:project,:ref,:digest,CAST(:content AS jsonb),:approval)"""),
            {
                "project": project_id,
                "ref": ref,
                "digest": digest,
                "content": json.dumps(definition),
                "approval": "sha256:" + "d" * 64,
            },
        )
    engine.dispose()
    profile_body = {
        **example["profile"],
        "project_id": project_id,
        "name": f"task-{layer.lower()}-{uuid4().hex[:8]}",
        "verifier_ref": ref,
        "verifier_digest": digest,
        "required_layers": [layer],
    }
    # 去掉 fixture 占位字段，只留 API 接受的键
    for drop in ("criteria", "cases"):
        profile_body.pop(drop, None)
    created = client.post(
        "/api/v1/verification-profiles",
        json=profile_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


def test_audit_assignment_layer_follows_semantic_profile(api, objects):
    store, _, _ = objects
    client, token, auth, goal, plan_activity = _start_goal(api, objects)
    semantic_id = _seed_task_layer_profile(
        client, auth, goal["project_id"], layer="SEMANTIC"
    )
    _drain_ready(client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE"))
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
              WHERE status='READY' AND kind='PLAN' AND id<>:id"""),
            {"id": plan_activity["id"]},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('CANCELLED','READY')"""
            ),
            {"id": plan_activity["id"]},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    plan = _plan_body(goal, semantic_id)
    plan["coverage"][0]["verification_profile_id"] = semantic_id
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    engine.dispose()
    exec_lease = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    client.app.state.objects = store

    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "layer-seal",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:layer-input",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert (
        client.post(
            f"/internal/v1/effects/{effect['id']}/dispatch",
            json={
                "lease": exec_lease["lease"],
                "effect_state_revision": effect["state_revision"],
            },
            headers=worker_auth,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/internal/v1/effects/{effect['id']}/receipts",
            json={
                "receipt_id": str(uuid4()),
                "effect_id": effect["id"],
                "producer_activity_id": activity_id,
                "producer_attempt_id": exec_lease["lease"]["attempt_id"],
                "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
                "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z",
                "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z",
                "exit_code": 0,
                "timed_out": False,
                "result_artifact_ids": [str(input_art.id)],
                "observed_outcome": "SUCCEEDED",
            },
            headers=worker_auth,
        ).status_code
        == 201
    )
    file_bytes = b"print('ok')\n"
    file_digest = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    Artifacts(engine, store).ingest_raw(
        project_id,
        file_digest,
        BytesIO(file_bytes),
        mime="text/x-python",
        producer_identity="test:layer-file",
    )
    snap = {
        "files": [{"path": "src/main.py", "digest": file_digest, "mode": "100644"}],
        "git_commit": None,
        "dependency_lock_digests": [],
        "submodules": [],
        "lfs_objects": [],
        "image_digests": [],
    }
    snap_raw = json.dumps(snap, separators=(",", ":")).encode()
    snap_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(snap_raw).hexdigest(),
        BytesIO(snap_raw),
        mime="application/json",
        producer_identity="test:layer-snap",
    )
    sealed = client.post(
        "/internal/v1/candidates/seal",
        json={
            "lease": exec_lease["lease"],
            "workspace_snapshot_artifact_id": str(snap_art.id),
            "verification_profile_ids": [semantic_id],
        },
        headers=worker_auth,
    )
    assert sealed.status_code == 201, sealed.text
    candidate = sealed.json()["data"]
    with create_engine(os.environ["RING_TEST_DATABASE_URL"]).begin() as db:
        rev = db.execute(
            text("SELECT state_revision FROM activities WHERE id=:id"),
            {"id": activity_id},
        ).scalar_one()
    assert (
        client.post(
            f"/internal/v1/activities/{activity_id}/outcomes",
            json={
                "lease": exec_lease["lease"],
                "expected_state_revision": rev,
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "evidence_ids": [str(input_art.id)],
                },
            },
            headers=worker_auth,
        ).status_code
        == 200
    )

    audit_subject = str(uuid4())
    _register_worker(audit_subject, kinds=("AUDIT",))
    audit_auth = {"Authorization": "Bearer " + token(audit_subject, ["worker"])}
    with create_engine(os.environ["RING_TEST_DATABASE_URL"]).begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='AUDIT' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**audit_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    assignment = claimed.json()["data"]["activity"]["verification_assignments"][0]
    assert assignment["layer"] == "SEMANTIC", assignment
    assert assignment["verification_profile_id"] == semantic_id
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"
