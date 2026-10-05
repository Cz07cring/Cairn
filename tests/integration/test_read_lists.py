"""GET /tasks/{id}/activities 与 GET /commands。"""

from uuid import uuid4

from test_claims import _drain_ready_plans
from test_replan import _publish_running


def test_list_task_activities_and_commands(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks", headers=auth).json()["data"]
    task = tasks[0]
    acts = client.get(f"/api/v1/tasks/{task['id']}/activities", headers=auth)
    assert acts.status_code == 200, acts.text
    data = acts.json()["data"]
    assert len(data) >= 1
    assert all(row["task_id"] == task["id"] for row in data)
    assert any(row["kind"] == "EXECUTE" for row in data)

    listed = client.get(
        "/api/v1/commands",
        params={"project_id": goal["project_id"], "goal_id": goal["id"]},
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    commands = listed.json()["data"]
    assert any(c["kind"] == "START" for c in commands)

    own = client.get("/api/v1/commands", headers=auth)
    assert own.status_code == 200, own.text
    assert any(c["goal_id"] == goal["id"] for c in own.json()["data"])

    effects = client.get(
        "/api/v1/effects",
        params={"project_id": goal["project_id"], "goal_id": goal["id"]},
        headers=auth,
    )
    assert effects.status_code == 200, effects.text

    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={"expected_state_revision": got["state_revision"], "reason": "cleanup"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    _drain_ready_plans(client, token)
