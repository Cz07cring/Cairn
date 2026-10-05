"""Checkpoint：PLAN 宿主采纳后可读列表；B03 形状拒绝。"""

from uuid import uuid4

from control_kernel.storage.claims import binding_digest_of
from test_claims import _register_worker, _start_goal


def _claim_and_bind_plan(client, token, goal):
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    activity_id = lease["activity"]["id"]
    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": compiled.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    return worker_auth, lease, bound.json()["data"]["context_digest"]


def test_plan_checkpoint_accepted_and_listed(api, objects):
    client, token, auth, goal, _plan = _start_goal(api, objects)
    worker_auth, lease, context_digest = _claim_and_bind_plan(client, token, goal)
    activity_id = lease["activity"]["id"]
    attempt_id = lease["lease"]["attempt_id"]

    empty = client.get(f"/api/v1/activities/{activity_id}/checkpoints", headers=auth)
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"] == []

    posted = client.post(
        f"/internal/v1/activities/{activity_id}/checkpoints",
        json={
            "lease": lease["lease"],
            "checkpoint": {
                "activity_id": activity_id,
                "attempt_id": attempt_id,
                "kind": "PLAN",
                "context_digest": context_digest,
                "completed_step_ids": [],
                "next_step_id": None,
                "candidate_manifest_id": None,
                "workspace_manifest_digest": None,
                "artifact_ids": [],
                "effect_ids": [],
                "session_ref": None,
            },
        },
        headers=worker_auth,
    )
    assert posted.status_code == 201, posted.text
    cp = posted.json()["data"]
    assert cp["kind"] == "PLAN"
    assert cp["context_digest"] == context_digest
    assert cp["content_digest"].startswith("sha256:")
    assert cp["schema_version"] == 3

    listed = client.get(f"/api/v1/activities/{activity_id}/checkpoints", headers=auth)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert len(rows) == 1
    assert rows[0]["id"] == cp["id"]

    # 同内容幂等：同一 digest 返回已有行
    again = client.post(
        f"/internal/v1/activities/{activity_id}/checkpoints",
        json={
            "lease": lease["lease"],
            "checkpoint": {
                "activity_id": activity_id,
                "attempt_id": attempt_id,
                "kind": "PLAN",
                "context_digest": context_digest,
                "completed_step_ids": [],
                "next_step_id": None,
                "candidate_manifest_id": None,
                "workspace_manifest_digest": None,
                "artifact_ids": [],
                "effect_ids": [],
                "session_ref": None,
            },
        },
        headers=worker_auth,
    )
    assert again.status_code == 201
    assert again.json()["data"]["id"] == cp["id"]


def test_plan_checkpoint_rejects_workspace_claim(api, objects):
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    worker_auth, lease, context_digest = _claim_and_bind_plan(client, token, goal)
    activity_id = lease["activity"]["id"]
    fake_digest = "sha256:" + ("ab" * 32)
    denied = client.post(
        f"/internal/v1/activities/{activity_id}/checkpoints",
        json={
            "lease": lease["lease"],
            "checkpoint": {
                "activity_id": activity_id,
                "attempt_id": lease["lease"]["attempt_id"],
                "kind": "PLAN",
                "context_digest": context_digest,
                "completed_step_ids": [],
                "next_step_id": None,
                "candidate_manifest_id": None,
                "workspace_manifest_digest": fake_digest,
                "artifact_ids": [],
                "effect_ids": [],
                "session_ref": None,
            },
        },
        headers=worker_auth,
    )
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "VALIDATION_ERROR"
