"""Spec 05 §3.7 rejects ambiguous JSON before model parsing can erase it."""

from uuid import uuid4


def test_write_json_rejects_duplicate_keys_and_noncanonical_numbers(api):
    client, token = api
    auth = {
        "Authorization": "Bearer " + token(str(uuid4()), ["admin"]),
        "Idempotency-Key": str(uuid4()),
        "Content-Type": "application/json",
    }
    for raw in [
        '{"name":"first","name":"second","repository_ref":"fixture"}',
        '{"name":"first","repository_ref":"fixture","x":-0}',
        '{"name":"first","repository_ref":"fixture","x":1.0}',
        '{"name":"first","repository_ref":"fixture","x":1e2}',
        '{"name":"first","repository_ref":"fixture","x":NaN}',
        '{"name":"\\ud800","repository_ref":"fixture"}',
    ]:
        response = client.post("/api/v1/projects", content=raw, headers=auth)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_resource_timestamps_use_frozen_utc_milliseconds(api):
    import re

    client, token = api
    headers = {
        "Authorization": "Bearer " + token(str(uuid4()), ["admin"]),
        "Idempotency-Key": str(uuid4()),
    }
    result = client.post(
        "/api/v1/projects", json={"name": "timestamp", "repository_ref": "fixture"}, headers=headers
    )
    assert result.status_code == 201
    for name in ["created_at", "updated_at"]:
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", result.json()["data"][name]
        )
