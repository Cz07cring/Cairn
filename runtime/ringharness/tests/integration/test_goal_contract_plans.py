"""PUT contract / POST plans 候选：不发布、不 DONE。"""

from uuid import uuid4

from test_claims import _start_goal
from test_goals import _ready_project
from test_plans import _plan_body


def test_put_contract_on_draft(api, objects):
    client, _token, auth, _project, goal_body = _ready_project(api, objects)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    assert goal["status"] == "DRAFT"
    assert goal["contract_revision"] == 1

    updated_body = {
        **goal_body,
        "objective": "更新后的目标说明",
    }
    put = client.put(
        f"/api/v1/goals/{goal['id']}/contract",
        json={
            "expected_state_revision": goal["state_revision"],
            "contract": updated_body,
            "reason": "澄清目标表述",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert put.status_code == 200, put.text
    data = put.json()["data"]
    assert data["contract_revision"] == 2
    assert data["state_revision"] == goal["state_revision"] + 1
    assert data["contract"]["objective"] == "更新后的目标说明"
    assert data["plan_revision"] is None
    assert data["status"] == "DRAFT"

    # PLANNING 中拒绝改合同
    started = client.post(
        f"/api/v1/goals/{data['id']}/start",
        json={"expected_state_revision": data["state_revision"], "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    denied = client.put(
        f"/api/v1/goals/{data['id']}/contract",
        json={
            "expected_state_revision": data["state_revision"] + 1,
            "contract": updated_body,
            "reason": "不应允许",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "INVALID_STATE"


def test_post_plan_candidate_does_not_publish(api, objects):
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    plan = _plan_body(goal, profile_id)
    posted = client.post(
        f"/api/v1/goals/{goal['id']}/plans",
        json=plan,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert posted.status_code == 201, posted.text
    candidate = posted.json()["data"]
    assert candidate["status"] == "CANDIDATE"
    assert candidate["plan_revision"] is None
    assert candidate["content_digest"].startswith("sha256:")

    listed = client.get(f"/api/v1/goals/{goal['id']}/plans", headers=auth)
    assert listed.status_code == 200
    assert any(row["id"] == candidate["id"] for row in listed.json()["data"])

    # 未发布：无 Task
    tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks", headers=auth)
    assert tasks.status_code == 200
    assert tasks.json()["data"] == []

    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "PLANNING"
    assert got["plan_revision"] is None
