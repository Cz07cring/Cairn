"""候选封存 + EXECUTE outcome → Task VERIFYING + AUDIT READY。"""

import hashlib
import json
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from test_effects import _publish_and_claim_execute


def test_seal_candidate_and_execute_outcome(api, objects):
    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    # 先把工程 effect 走完（否则不能封存）
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
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:input",
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
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200
    from datetime import UTC, datetime

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
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(input_art.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text

    file_bytes = b"print('ok')\n"
    file_digest = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    Artifacts(engine, store).ingest_raw(
        project_id,
        file_digest,
        BytesIO(file_bytes),
        mime="text/x-python",
        producer_identity="test:candidate-file",
    )
    snapshot = {
        "files": [{"path": "src/main.py", "digest": file_digest, "mode": "100644"}],
        "git_commit": None,
        "dependency_lock_digests": [],
        "submodules": [],
        "lfs_objects": [],
        "image_digests": [],
    }
    snap_raw = json.dumps(snapshot, separators=(",", ":")).encode()
    snap_digest = "sha256:" + hashlib.sha256(snap_raw).hexdigest()
    snap_art = Artifacts(engine, store).ingest_raw(
        project_id,
        snap_digest,
        BytesIO(snap_raw),
        mime="application/json",
        producer_identity="test:snapshot",
    )
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    sealed = client.post(
        "/internal/v1/candidates/seal",
        json={
            "lease": exec_lease["lease"],
            "workspace_snapshot_artifact_id": str(snap_art.id),
            "verification_profile_ids": [profile_id],
        },
        headers=worker_auth,
    )
    assert sealed.status_code == 201, sealed.text
    candidate = sealed.json()["data"]
    assert candidate["files"][0]["path"] == "src/main.py"
    assert candidate["content_digest"].startswith("sha256:")

    got = client.get(f"/api/v1/candidates/{candidate['id']}", headers=auth)
    assert got.status_code == 200
    assert got.json()["data"]["id"] == candidate["id"]

    done = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": exec_lease["lease"],
            "expected_state_revision": exec_lease["activity"]["state_revision"],
            "outcome": {
                "candidate_manifest_id": candidate["id"],
                "evidence_ids": [str(input_art.id)],
            },
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "SUCCEEDED"

    task_id = exec_lease["activity"]["task_id"]
    task = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task["status"] == "VERIFYING"

    audits = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "AUDIT"},
        headers=auth,
    )
    assert audits.status_code == 200
    assert len(audits.json()["data"]) == 1
    assert audits.json()["data"][0]["status"] == "READY"
    assert audits.json()["data"][0]["target"]["type"] == "CANDIDATE"
    assert audits.json()["data"][0]["target"]["id"] == candidate["id"]
