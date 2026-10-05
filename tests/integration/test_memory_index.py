"""INDEX_MEMORY：queue → claim → outcome → PROPOSED（无源证据）→ VERIFIED。"""

from uuid import uuid4

from test_claims import _register_worker, _start_goal


def test_index_memory_empty_evidence_proposed_becomes_verified(api, objects):
    client, token, auth, goal, plan = _start_goal(api, objects)
    project_id = goal["project_id"]
    activity_id = plan["id"]

    # 无 PROPOSED 时拒绝排队
    empty = client.post(
        f"/internal/v1/projects/{project_id}/memory-index",
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert empty.status_code == 422, empty.text
    assert "PROPOSED" in empty.json()["error"]["message"]

    created = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "fact",
            "statement": "空源证据可诚实晋升",
            "source_evidence_ids": [],
            "confidence_bp": 100,  # 低置信度也不得挡 VERIFIED（不看 confidence_bp）
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    memory = created.json()["data"]
    assert memory["status"] == "PROPOSED"

    # operator 不可触发索引
    denied = client.post(
        f"/internal/v1/projects/{project_id}/memory-index",
        headers={
            "Authorization": "Bearer " + token(str(uuid4()), ["operator"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert denied.status_code == 403

    key = str(uuid4())
    started = client.post(
        f"/internal/v1/projects/{project_id}/memory-index",
        headers={**auth, "Idempotency-Key": key},
    )
    assert started.status_code == 202, started.text
    command = started.json()["data"]
    assert command["kind"] == "INDEX_MEMORY"
    assert command["status"] == "RUNNING"
    index_activity_id = command["result"]["activity_id"]

    # 幂等
    again = client.post(
        f"/internal/v1/projects/{project_id}/memory-index",
        headers={**auth, "Idempotency-Key": key},
    )
    assert again.status_code == 202
    assert again.json()["data"]["id"] == command["id"]

    # 进行中不可再排
    conflict = client.post(
        f"/internal/v1/projects/{project_id}/memory-index",
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert conflict.status_code == 422
    assert "进行中" in conflict.json()["error"]["message"]

    subject = str(uuid4())
    _register_worker(subject, kinds=("INDEX_MEMORY",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["INDEX_MEMORY"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == index_activity_id
    assert lease["activity"]["kind"] == "INDEX_MEMORY"
    assert lease["activity"]["target"]["type"] == "MEMORY_INDEX"
    assert lease["activity"]["target"]["id"] == project_id
    assert lease["activity"]["goal_id"] is None
    assert lease["activity"]["task_id"] is None

    index_version = "fixture-v1"
    done = client.post(
        f"/internal/v1/activities/{index_activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "record_ids": [memory["id"]],
                "index_version": index_version,
                "evidence_ids": [],
            },
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "SUCCEEDED"

    listed = client.get("/api/v1/memories", params={"project_id": project_id}, headers=auth)
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json()["data"]}
    assert by_id[memory["id"]]["status"] == "VERIFIED"

    cmd = client.get(f"/api/v1/commands/{command['id']}", headers=auth)
    assert cmd.status_code == 200, cmd.text
    assert cmd.json()["data"]["status"] == "SUCCEEDED"
    assert cmd.json()["data"]["result"]["index_version"] == index_version
    assert cmd.json()["data"]["result"]["record_ids"] == [memory["id"]]
