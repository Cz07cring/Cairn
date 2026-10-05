"""Tasks 列表 cursor 分页（doc/05：cursor,limit）。"""

from uuid import uuid4

from test_claims import _register_worker, _start_goal
from test_plans import _plan_body


def _plan_body_two_tasks(goal, task_profile_id: str):
    """两节点计划，便于 limit=1 翻页。"""
    base = _plan_body(goal, task_profile_id)
    t1 = base["tasks"][0]
    t2_id = str(uuid4())
    criterion = goal["contract"]["success_criteria"][0]
    t2 = {
        **t1,
        "id": t2_id,
        "contract": {
            **t1["contract"],
            "objective": "第二任务",
            "depends_on": [t1["id"]],
        },
    }
    base["tasks"] = [t1, t2]
    base["coverage"] = [
        base["coverage"][0],
        {
            "goal_criterion_id": criterion["id"],
            "task_id": t2_id,
            "task_acceptance_id": "A1",
            "verification_profile_id": criterion["verification_profile_id"],
        },
    ]
    return base


def test_list_goal_tasks_cursor_pagination(api, objects):
    """limit=1 时返回 next_cursor；再取第二页；坏 cursor → 400。"""
    client, token, auth, goal, _plan_activity = _start_goal(api, objects)
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
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": _plan_body_two_tasks(goal, profile_id)},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text

    page1 = client.get(
        f"/api/v1/goals/{goal['id']}/tasks",
        params={"limit": 1},
        headers=auth,
    )
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert len(body1["data"]) == 1
    cursor = body1["meta"]["next_cursor"]
    assert cursor is not None and cursor != ""

    page2 = client.get(
        f"/api/v1/goals/{goal['id']}/tasks",
        params={"limit": 1, "cursor": cursor},
        headers=auth,
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert len(body2["data"]) == 1
    assert body2["data"][0]["id"] != body1["data"][0]["id"]
    assert body2["meta"].get("next_cursor") in (None, "")

    bad = client.get(
        f"/api/v1/goals/{goal['id']}/tasks",
        params={"limit": 1, "cursor": "not-a-cursor"},
        headers=auth,
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_REQUEST"
