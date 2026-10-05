"""Real HTTP/auth/PG project behavior."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


def test_project_creation_is_durable_scoped_and_idempotent(api):
    client, token = api
    subject = str(uuid4())
    key = str(uuid4())
    headers = {
        "Authorization": "Bearer " + token(subject, ["admin", "viewer"]),
        "Idempotency-Key": key,
    }
    body = {"name": "测试项目", "repository_ref": "fixture"}
    assert client.get("/api/v1/projects").status_code == 401

    def create(_):
        return client.post("/api/v1/projects", json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert all(r.status_code == 201 for r in results)
    ids = {r.json()["data"]["id"] for r in results}
    assert len(ids) == 1
    listed = client.get("/api/v1/projects", headers=headers).json()["data"]
    assert [p["id"] for p in listed] == list(ids)
    assert (
        client.get(
            "/api/v1/projects",
            headers={"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])},
        ).json()["data"]
        == []
    )
    assert (
        client.post(
            "/api/v1/projects", json={**body, "name": "changed"}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/projects", json={**body, "repository_ref": "/etc"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/projects",
            json=body,
            headers={
                "Authorization": "Bearer " + token(subject, ["viewer"]),
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 403
    )


def test_pagination_and_cursor_are_bound_to_the_principal(api):
    client, token = api
    principal = str(uuid4())
    auth = {"Authorization": "Bearer " + token(principal, ["admin", "viewer"])}
    for name in ["one", "two", "three"]:
        assert (
            client.post(
                "/api/v1/projects",
                json={"name": name, "repository_ref": "fixture"},
                headers={**auth, "Idempotency-Key": str(uuid4())},
            ).status_code
            == 201
        )
    first = client.get("/api/v1/projects?limit=2", headers=auth).json()
    assert [p["name"] for p in first["data"]] == ["one", "two"]
    cursor = first["meta"]["next_cursor"]
    assert cursor
    second = client.get(
        "/api/v1/projects", params={"limit": 2, "cursor": cursor}, headers=auth
    ).json()
    assert [p["name"] for p in second["data"]] == ["three"]
    assert second["meta"]["next_cursor"] is None
    outsider = {"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])}
    assert (
        client.get("/api/v1/projects", params={"cursor": cursor}, headers=outsider).status_code
        == 400
    )
    assert (
        client.get("/api/v1/projects", params={"cursor": "%%%invalid"}, headers=auth).status_code
        == 400
    )


def test_readiness_and_validation_do_not_expose_secrets(api):
    client, token = api
    assert client.get("/health/live").json() == {"alive": True}
    assert client.get("/health/ready").status_code == 401
    viewer = {"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])}
    assert client.get("/health/ready", headers=viewer).status_code == 403
    ops = {"Authorization": "Bearer " + token(str(uuid4()), ["ops"])}
    assert client.get("/health/ready", headers=ops).json()["scope"] == "project-config-api-only"
    bad = client.post("/api/v1/projects", json={"secret": "never-echo-this"}, headers=viewer)
    assert bad.status_code == 422
    assert "never-echo-this" not in bad.text
