"""Immutable, scoped configuration is required before accepting a Goal contract."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


def setup_project(api):
    client, token = api
    auth = {"Authorization": "Bearer " + token(str(uuid4()), ["admin", "viewer"])}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Policy test", "repository_ref": "fixture"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]["id"]
    body = {
        "project_id": project,
        "name": "isolated",
        "allowed_tools": ["read_file", "test"],
        "allowed_paths": ["src/**"],
        "protected_paths": ["tests/holdout/**"],
        "network_allowlist": [],
        "external_actions": [{"action": "deploy", "mode": "DENY"}],
        "secret_scope_refs": [],
    }
    return client, token, auth, body


def test_policy_replay_is_canonical_and_new_versions_are_immutable(api):
    client, _, auth, body = setup_project(api)
    headers = {**auth, "Idempotency-Key": str(uuid4())}
    first = client.post("/api/v1/policies", json=body, headers=headers)
    assert first.status_code == 201, first.text
    item = first.json()["data"]
    assert item["version"] == 1
    assert item["content_digest"].startswith("sha256:")
    replay = client.post(
        "/api/v1/policies", json={**body, "allowed_tools": ["test", "read_file"]}, headers=headers
    )
    assert replay.json()["data"] == item
    conflict = client.post("/api/v1/policies", json={**body, "allowed_tools": []}, headers=headers)
    assert conflict.status_code == 409
    second = client.post(
        "/api/v1/policies",
        json={**body, "allowed_tools": []},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert second.status_code == 201
    assert second.json()["data"]["version"] == 2
    listed = client.get("/api/v1/policies", params={"project_id": body["project_id"]}, headers=auth)
    assert listed.status_code == 200
    assert listed.json()["data"][0] == item


def test_policy_requires_project_scope_and_rejects_duplicate_sets(api):
    client, token, auth, body = setup_project(api)
    outsider = {
        "Authorization": "Bearer " + token(str(uuid4()), ["admin", "viewer"]),
        "Idempotency-Key": str(uuid4()),
    }
    assert client.post("/api/v1/policies", json=body, headers=outsider).status_code == 404
    assert (
        client.get(
            "/api/v1/policies", params={"project_id": body["project_id"]}, headers=outsider
        ).status_code
        == 404
    )
    invalid = {**body, "allowed_tools": ["read_file", "read_file"]}
    assert (
        client.post(
            "/api/v1/policies", json=invalid, headers={**auth, "Idempotency-Key": str(uuid4())}
        ).status_code
        == 422
    )


def test_concurrent_policy_versions_have_unique_contiguous_numbers(api):
    client, _, auth, body = setup_project(api)

    def create(i):
        return client.post(
            "/api/v1/policies",
            json={**body, "allowed_tools": [f"tool-{i}"]},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert all(r.status_code == 201 for r in results)
    assert sorted(r.json()["data"]["version"] for r in results) == [1, 2, 3, 4]


def test_policy_pagination_cannot_cross_project_or_principal(api):
    client, _token, auth, body = setup_project(api)
    for name in ["a", "b", "c"]:
        result = client.post(
            "/api/v1/policies",
            json={**body, "name": name},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 201
    first = client.get(
        "/api/v1/policies", params={"project_id": body["project_id"], "limit": 2}, headers=auth
    ).json()
    cursor = first["meta"]["next_cursor"]
    assert cursor
    rest = client.get(
        "/api/v1/policies",
        params={"project_id": body["project_id"], "cursor": cursor, "limit": 2},
        headers=auth,
    ).json()
    assert [p["config"]["name"] for p in first["data"] + rest["data"]] == ["a", "b", "c"]
    assert rest["meta"]["next_cursor"] is None
    assert (
        client.get(
            "/api/v1/policies", params={"project_id": str(uuid4()), "cursor": cursor}, headers=auth
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/policies",
            params={"project_id": body["project_id"], "cursor": "not-a-cursor"},
            headers=auth,
        ).status_code
        == 400
    )
