"""Creating model configuration must never imply a successful capability probe."""

from uuid import uuid4


def test_model_profile_is_versioned_scoped_and_unverified(api):
    client, token = api
    auth = {"Authorization": "Bearer " + token(str(uuid4()), ["admin", "viewer"])}
    project = client.post(
        "/api/v1/projects",
        json={"name": "models", "repository_ref": "fixture"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]["id"]
    body = {
        "project_id": project,
        "name": "local",
        "local_provider_ref": "local-qwen",
        "model_id": "test-model",
        "context_window": 8192,
        "output_reserve": 1024,
        "inference_slots": 1,
        "cloud_provider_refs": [],
        "cloud_mode": "DENY",
    }
    headers = {**auth, "Idempotency-Key": str(uuid4())}
    created = client.post("/api/v1/model-profiles", json=body, headers=headers)
    assert created.status_code == 201, created.text
    value = created.json()["data"]
    assert value["capability_status"] == "UNVERIFIED"
    assert value["probe_evidence_ids"] == []
    assert client.post("/api/v1/model-profiles", json=body, headers=headers).json()["data"] == value
    listing = client.get("/api/v1/model-profiles", params={"project_id": project}, headers=auth)
    assert listing.json()["data"] == [value]
    invalid = client.post(
        "/api/v1/model-profiles",
        json={**body, "output_reserve": 8192},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert invalid.status_code == 422
    outsider = {"Authorization": "Bearer " + token(str(uuid4()), ["admin"])}
    assert (
        client.get(
            "/api/v1/model-profiles", params={"project_id": project}, headers=outsider
        ).status_code
        == 404
    )
