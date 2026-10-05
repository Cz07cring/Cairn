"""AUDIT outcome → Task DONE；绝不把 Goal 写成 DONE。"""

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from test_claims import _register_worker
from test_effects import _publish_and_claim_execute
from verification_run_helpers import post_verification_run as _post_verification_run
from verification_run_helpers import profile_verifier_digest as _profile_verifier_digest
from verification_run_helpers import receipt_artifact as _receipt_artifact


def _seal_and_finish_execute(api, objects):
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
    return client, token, auth, goal, candidate, exec_lease["activity"]["task_id"]


def test_audit_pass_marks_task_done_not_goal(api, objects):
    store, _, _ = objects
    client, token, auth, goal, candidate, task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}

    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["kind"] == "AUDIT"
    assert lease["activity"]["target"]["id"] == candidate["id"]
    assignment = lease["activity"]["verification_assignments"][0]
    binding = lease["activity"]["binding"]

    project_id = UUID(lease["activity"]["project_id"])
    receipt_id = _receipt_artifact(client.app.state.engine, store, project_id)
    verifier_digest = _profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run_id = _post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
    )

    outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "CANDIDATE",
                "audit": {
                    "subject_candidate_manifest_id": candidate["id"],
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "task_contract_revision": binding["task_contract_revision"],
                    "verification_profile_id": assignment["verification_profile_id"],
                    "layer": assignment["layer"],
                    "audit_round": assignment["audit_round"],
                    "verifier_run_ids": [run_id],
                    "verdict": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": "A1",
                            "verdict": "PASS",
                            "evidence_ids": [],
                            "reason": "机械检查通过",
                        }
                    ],
                    "evidence_ids": [],
                    "reason": "验收通过",
                },
            },
        },
        headers=worker_auth,
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["data"]["status"] == "SUCCEEDED"

    task = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task["status"] == "DONE"

    audits = client.get(f"/api/v1/tasks/{task_id}/audits", headers=auth)
    assert audits.status_code == 200, audits.text
    assert len(audits.json()["data"]) == 1
    assert audits.json()["data"][0]["verdict"] == "PASS"
    assert audits.json()["data"][0]["content_digest"].startswith("sha256:")

    goal_audits = client.get(f"/api/v1/goals/{goal['id']}/audits", headers=auth)
    assert goal_audits.status_code == 200, goal_audits.text
    items = goal_audits.json()["data"]
    assert len(items) == 1
    assert items[0]["record_type"] == "CANDIDATE_AUDIT"
    assert items[0]["audit"]["id"] == audits.json()["data"][0]["id"]
    assert items[0]["aggregation"]["verdict"] == "PASS"
    assert items[0]["aggregation"]["subject_candidate_manifest_id"] == candidate["id"]
    assert items[0]["aggregation"]["required_set_digest"].startswith("sha256:")
    assert len(items[0]["aggregation"]["accepted_assessment_ids"]) == 1

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"

    snap = client.get(f"/api/v1/goals/{goal['id']}/snapshot", headers=auth)
    assert snap.status_code == 200, snap.text
    feedbacks = snap.json()["data"]["planning_feedback"]
    assert len(feedbacks) == 1
    assert feedbacks[0]["verdict"] == "PASS"
    assert feedbacks[0]["candidate_manifest_id"] == candidate["id"]
    assert feedbacks[0]["aggregation_digest"].startswith("sha256:")
    assert snap.json()["data"]["feedback_truncated"] is False


def test_audit_rejects_unknown_verifier_run_id(api, objects):
    client, token, _auth, _goal, candidate, _task_id = _seal_and_finish_execute(api, objects)
    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assignment = lease["activity"]["verification_assignments"][0]
    binding = lease["activity"]["binding"]
    outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "CANDIDATE",
                "audit": {
                    "subject_candidate_manifest_id": candidate["id"],
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "task_contract_revision": binding["task_contract_revision"],
                    "verification_profile_id": assignment["verification_profile_id"],
                    "layer": assignment["layer"],
                    "audit_round": assignment["audit_round"],
                    "verifier_run_ids": [str(uuid4())],
                    "verdict": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": "A1",
                            "verdict": "PASS",
                            "evidence_ids": [],
                            "reason": "机械检查通过",
                        }
                    ],
                    "evidence_ids": [],
                    "reason": "验收通过",
                },
            },
        },
        headers=worker_auth,
    )
    assert outcome.status_code == 422, outcome.text
    assert outcome.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "VerificationRun" in outcome.json()["error"]["message"]
