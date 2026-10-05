"""Plans 列表 cursor 分页（doc/05：cursor,limit）。"""

from uuid import uuid4

from test_claims import _register_worker, _start_goal
from test_plans import _plan_body


def test_list_goal_plans_cursor_pagination(api, objects):
    """publish 一条 + POST 候选一条 → ≥2；limit=1 翻页；坏 cursor → 400。"""
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
    plan = _plan_body(goal, profile_id)
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text

    running = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    # 人工候选计划：第二行（CANDIDATE），供分页
    candidate = {
        **plan,
        "expected_plan_revision": running["plan_revision"],
        "reason": "cursor page candidate",
        "tasks": [
            {
                **plan["tasks"][0],
                "id": str(uuid4()),
            }
        ],
    }
    candidate["coverage"] = [
        {
            **plan["coverage"][0],
            "task_id": candidate["tasks"][0]["id"],
        }
    ]
    posted = client.post(
        f"/api/v1/goals/{goal['id']}/plans",
        json=candidate,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert posted.status_code == 201, posted.text

    page1 = client.get(
        f"/api/v1/goals/{goal['id']}/plans",
        params={"limit": 1},
        headers=auth,
    )
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert len(body1["data"]) == 1
    cursor = body1["meta"]["next_cursor"]
    assert cursor

    page2 = client.get(
        f"/api/v1/goals/{goal['id']}/plans",
        params={"limit": 1, "cursor": cursor},
        headers=auth,
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert len(body2["data"]) == 1
    assert body2["data"][0]["id"] != body1["data"][0]["id"]
    assert body2["meta"].get("next_cursor") in (None, "")

    bad = client.get(
        f"/api/v1/goals/{goal['id']}/plans",
        params={"limit": 1, "cursor": "broken"},
        headers=auth,
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_REQUEST"
