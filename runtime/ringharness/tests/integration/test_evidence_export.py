"""POST /goals/{id}/evidence-exports：排队 READY EXPORT_EVIDENCE，不捏造 artifact。"""

from uuid import uuid4

from test_claims import _drain_ready_plans
from test_finalization import _goal_done_with_release


def test_evidence_export_internal_copy_queues_ready_activity(api, objects):
    client, token, auth, goal_done, release_id, _candidate, worker_auth = _goal_done_with_release(
        api, objects
    )
    release = client.get(f"/api/v1/goals/{goal_done['id']}/release", headers=auth)
    assert release.status_code == 200, release.text
    release_digest = release.json()["data"]["manifest"]["content_digest"]

    key = str(uuid4())
    started = client.post(
        f"/api/v1/goals/{goal_done['id']}/evidence-exports",
        json={"release_manifest_id": release_id, "trust_mode": "INTERNAL_COPY"},
        headers={**auth, "Idempotency-Key": key},
    )
    assert started.status_code == 202, started.text
    command = started.json()["data"]
    assert command["kind"] == "EXPORT_EVIDENCE"
    assert command["status"] == "RUNNING"
    result = command["result"]
    assert result["trust_mode"] == "INTERNAL_COPY"
    assert result["activity_id"]
    assert "artifact_id" not in result

    activity = client.get(f"/api/v1/activities/{result['activity_id']}", headers=auth)
    assert activity.status_code == 200, activity.text
    body = activity.json()["data"]
    assert body["status"] == "READY"
    assert body["kind"] == "EXPORT_EVIDENCE"
    assert body["target"]["type"] == "RELEASE_EXPORT"
    assert body["target"]["id"] == release_id
    assert body["goal_id"] == goal_done["id"]
    assert body["task_id"] is None
    assert body["binding"]["subject_digest"] == release_digest
    assert body["verification_assignments"][0]["trust_mode"] == "INTERNAL_COPY"

    # 同键幂等
    again = client.post(
        f"/api/v1/goals/{goal_done['id']}/evidence-exports",
        json={"release_manifest_id": release_id, "trust_mode": "INTERNAL_COPY"},
        headers={**auth, "Idempotency-Key": key},
    )
    assert again.status_code == 202, again.text
    assert again.json()["data"]["id"] == command["id"]
    assert again.json()["data"]["result"]["activity_id"] == result["activity_id"]

    # Goal DONE 后仍可 claim EXPORT_EVIDENCE；lease 可见 trust_mode
    _drain_ready_plans(client, token)
    engine_restore = client.app.state.engine
    from sqlalchemy import text

    with engine_restore.begin() as db:
        db.execute(
            text(
                "UPDATE activities SET status='READY', updated_at=clock_timestamp() WHERE id=:id"
            ),
            {"id": result["activity_id"]},
        )
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXPORT_EVIDENCE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["lease"] is not None
    assert lease["activity"]["id"] == result["activity_id"]
    assert lease["activity"]["kind"] == "EXPORT_EVIDENCE"
    assert lease["activity"]["verification_assignments"][0]["trust_mode"] == "INTERNAL_COPY"
    assert lease["activity"]["binding"]["subject_digest"] == release_digest


def test_evidence_export_offline_without_signing_env_fails_closed(api, objects, monkeypatch):
    client, _token, auth, goal_done, release_id, _candidate, _worker_auth = _goal_done_with_release(
        api, objects
    )
    monkeypatch.delenv("RING_ATTESTATION_PROVIDER_REF", raising=False)
    monkeypatch.delenv("RING_TRUST_BUNDLE_REF", raising=False)
    denied = client.post(
        f"/api/v1/goals/{goal_done['id']}/evidence-exports",
        json={"release_manifest_id": release_id, "trust_mode": "OFFLINE_VERIFIABLE"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 503, denied.text
    err = denied.json()["error"]
    assert err["code"] == "SIGNING_UNAVAILABLE"
    assert "签名" in err["message"] or "信任" in err["message"]


def test_evidence_export_offline_with_signing_env_queues(api, objects, monkeypatch):
    client, _token, auth, goal_done, release_id, _candidate, _worker_auth = _goal_done_with_release(
        api, objects
    )
    monkeypatch.setenv("RING_ATTESTATION_PROVIDER_REF", "test.attestation.provider")
    monkeypatch.setenv("RING_TRUST_BUNDLE_REF", "test.trust.bundle")
    started = client.post(
        f"/api/v1/goals/{goal_done['id']}/evidence-exports",
        json={"release_manifest_id": release_id, "trust_mode": "OFFLINE_VERIFIABLE"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    data = started.json()["data"]
    assert data["kind"] == "EXPORT_EVIDENCE"
    assert data["result"]["trust_mode"] == "OFFLINE_VERIFIABLE"
    assert "artifact_id" not in data["result"]
    activity = client.get(f"/api/v1/activities/{data['result']['activity_id']}", headers=auth)
    assert activity.json()["data"]["verification_assignments"][0]["trust_mode"] == (
        "OFFLINE_VERIFIABLE"
    )


def test_evidence_export_rejects_wrong_release_id(api, objects):
    client, _token, auth, goal_done, _release_id, _candidate, _worker_auth = _goal_done_with_release(
        api, objects
    )
    wrong = client.post(
        f"/api/v1/goals/{goal_done['id']}/evidence-exports",
        json={"release_manifest_id": str(uuid4()), "trust_mode": "INTERNAL_COPY"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert wrong.status_code in (404, 422), wrong.text
    assert wrong.json()["error"]["code"] in ("NOT_FOUND", "VALIDATION_ERROR")
