"""ReadModel：Goal snapshot / SSE events / system status。"""

import json
from uuid import UUID, uuid4

from control_kernel.storage.events import append_goal_event
from sqlalchemy import event, text
from test_claims import _register_worker, _start_goal


def _read_first_sse_event(client, url: str, headers: dict, params: dict) -> dict:
    """消费首个 data 帧后立即关闭流，避免 TestClient 挂在长连接上。"""
    with client.stream("GET", url, params=params, headers=headers) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        buf = ""
        for chunk in response.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                fields: dict[str, str] = {}
                for line in block.split("\n"):
                    if line.startswith(":") or not line:
                        continue
                    key, _, value = line.partition(":")
                    fields[key] = value.lstrip()
                if "data" in fields:
                    event = json.loads(fields["data"])
                    assert fields.get("id") == event["seq"]
                    assert fields.get("event") == event["type"]
                    response.close()
                    return event
        raise AssertionError("SSE 未返回事件帧")


def test_goal_snapshot_and_sse_events_after_start(api, objects):
    client, _token, auth, goal, plan = _start_goal(api, objects)
    with client.app.state.engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  wait_reason,wake_at,wait_deadline_at,resume_state,retry_count,current_attempt_id,
                  resources,created_at,updated_at)
                SELECT
                  :id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,'SUCCEEDED',state_revision,
                  depends_on_activity_ids,NULL,NULL,NULL,NULL,retry_count,NULL,
                  resources,created_at - INTERVAL '1 day',updated_at - INTERVAL '1 day'
                FROM activities WHERE id=:source"""
            ),
            [
                {"id": uuid4(), "source": UUID(plan["id"])}
                for _ in range(1000)
            ],
        )
    snap = client.get(f"/api/v1/goals/{goal['id']}/snapshot", headers=auth)
    assert snap.status_code == 200, snap.text
    data = snap.json()["data"]
    assert data["goal"]["id"] == goal["id"]
    assert data["goal"]["status"] == "PLANNING"
    assert data["plan"] is None
    assert data["latest_seq"] == "1"
    assert data["feedback_truncated"] is False
    assert isinstance(data["activities"], list)
    assert [activity["id"] for activity in data["activities"]] == [plan["id"]]
    assert data["activities"][0]["kind"] == "PLAN"

    got = _read_first_sse_event(
        client,
        f"/api/v1/goals/{goal['id']}/events",
        auth,
        {"after_seq": "0"},
    )
    assert got["type"] == "GOAL_STATE_CHANGED"
    assert got["seq"] == "1"
    assert got["payload"] == {"resource_type": "GOAL", "change": "INVALIDATE"}

    conflict = client.get(
        f"/api/v1/goals/{goal['id']}/events",
        params={"after_seq": "0"},
        headers={**auth, "Last-Event-ID": "1"},
    )
    assert conflict.status_code == 400

    oversized_header = client.get(
        f"/api/v1/goals/{goal['id']}/events",
        headers={**auth, "Last-Event-ID": "9" * 5000},
    )
    assert oversized_header.status_code == 400
    assert oversized_header.json()["error"]["code"] == "INVALID_REQUEST"


def test_goal_snapshot_keeps_objects_and_cursor_from_same_database_snapshot(
    api, objects
):
    client, _token, auth, goal, plan = _start_goal(api, objects)
    concurrent_commit_completed = False

    def commit_activity_change_after_snapshot_read(
        connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal concurrent_commit_completed
        if (
            "SELECT * FROM activities" not in statement
            or "WHERE goal_id=" not in statement
            or concurrent_commit_completed
        ):
            return
        concurrent_commit_completed = True
        with client.app.state.engine.begin() as writer:
            state_revision = writer.execute(
                text(
                    """UPDATE activities
                    SET status='RUNNING', state_revision=state_revision+1,
                        updated_at=clock_timestamp()
                    WHERE id=:id
                    RETURNING state_revision"""
                ),
                {"id": UUID(plan["id"])},
            ).scalar_one()
            append_goal_event(
                writer,
                project_id=UUID(goal["project_id"]),
                goal_id=UUID(goal["id"]),
                event_type="ACTIVITY_STATE_CHANGED",
                entity_id=UUID(plan["id"]),
                entity_state_revision=state_revision,
                resource_type="ACTIVITY",
            )

    event.listen(
        client.app.state.engine,
        "after_cursor_execute",
        commit_activity_change_after_snapshot_read,
    )
    try:
        response = client.get(
            f"/api/v1/goals/{goal['id']}/snapshot",
            headers=auth,
        )
    finally:
        event.remove(
            client.app.state.engine,
            "after_cursor_execute",
            commit_activity_change_after_snapshot_read,
        )

    assert response.status_code == 200, response.text
    assert concurrent_commit_completed is True
    snapshot = response.json()["data"]
    plan_activity = next(row for row in snapshot["activities"] if row["id"] == plan["id"])
    assert plan_activity["status"] == "READY"
    assert snapshot["latest_seq"] == "1"

    refreshed = client.get(
        f"/api/v1/goals/{goal['id']}/snapshot",
        headers=auth,
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_snapshot = refreshed.json()["data"]
    refreshed_activity = next(
        row for row in refreshed_snapshot["activities"] if row["id"] == plan["id"]
    )
    assert refreshed_activity["status"] == "RUNNING"
    assert refreshed_snapshot["latest_seq"] == "2"


def test_system_status_reports_trust(api, objects):
    client, token, auth, goal, plan = _start_goal(api, objects)
    store, _, _ = objects
    client.app.state.objects = store
    subject = str(uuid4())
    worker_id = _register_worker(subject)
    with client.app.state.engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities
                SET resources=jsonb_set(resources, '{browser_slots}', '2'::jsonb)
                WHERE id=:id"""
            ),
            {"id": UUID(plan["id"])},
        )
        db.execute(
            text("UPDATE workers SET browser_slots=2 WHERE id=:id"),
            {"id": UUID(worker_id)},
        )
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(subject, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["lease"] is not None

    status = client.get(
        "/api/v1/system/status",
        params={"project_id": goal["project_id"]},
        headers=auth,
    )
    assert status.status_code == 200, status.text
    body = status.json()["data"]
    assert body["trust"]["project_id"] == goal["project_id"]
    assert body["trust"]["status"] == "OPEN"
    assert body["trust"]["trust_revision"] == "1"
    names = {c["name"] for c in body["components"]}
    assert "postgres" in names
    assert "object_store" in names
    assert "verification_obligation_quarantine" in names
    object_store = next(c for c in body["components"] if c["name"] == "object_store")
    assert object_store["state"] == "HEALTHY"
    assert object_store["reason_code"] is None
    obl = next(
        c for c in body["components"] if c["name"] == "verification_obligation_quarantine"
    )
    assert obl["state"] == "HEALTHY"
    assert obl["reason_code"] is None
    assert body["resources"]["browsers"]["used"] == 2
    assert body["resources"]["stale"] is True
    assert body["stale"] is False


def test_system_status_reports_configured_but_unreachable_object_store(api, objects):
    client, _token, auth, goal, _plan = _start_goal(api, objects)

    class UnreachableObjectStore:
        def probe(self) -> None:
            raise OSError("fixture object store unavailable")

    original_store = client.app.state.objects
    client.app.state.objects = UnreachableObjectStore()
    try:
        status = client.get(
            "/api/v1/system/status",
            params={"project_id": goal["project_id"]},
            headers=auth,
        )
    finally:
        client.app.state.objects = original_store

    assert status.status_code == 200, status.text
    object_store = next(
        c for c in status.json()["data"]["components"] if c["name"] == "object_store"
    )
    assert object_store["state"] == "UNAVAILABLE"
    assert object_store["reason_code"] == "OBJECT_STORE_UNREACHABLE"
