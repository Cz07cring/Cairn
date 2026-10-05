"""PLAN outcome 发布 Plan / Task；不执行工程工具。"""

from uuid import uuid4

from test_claims import _register_worker, _start_goal


def _plan_body(goal, task_profile_id: str):
    task_id = str(uuid4())
    criterion = goal["contract"]["success_criteria"][0]
    return {
        "expected_plan_revision": None,
        "reason": "initial plan",
        "tasks": [
            {
                "id": task_id,
                "contract": {
                    "objective": "修复并验证",
                    "depends_on": [],
                    "input_artifact_ids": [],
                    "deliverables": [{"kind": "patch", "required": True}],
                    "acceptance": [
                        {
                            "id": "A1",
                            "description": "机械验收",
                            "required": True,
                            "verification_profile_id": task_profile_id,
                        }
                    ],
                    "covers_goal_criterion_ids": [criterion["id"]],
                    "allowed_paths": ["src/**"],
                    "protected_paths": [],
                    "required_capabilities": [],
                    "budget": goal["contract"]["budget"],
                    "retry_policy": {
                        "max_execution_rounds": 2,
                        "max_audit_attempts_per_candidate": 2,
                        "max_activity_retries": 1,
                    },
                    "resources": {
                        "cpu_millicores": 100,
                        "memory_bytes": 268435456,
                        "disk_bytes": 67108864,
                        "model_slots": 0,
                        "browser_slots": 0,
                        "exclusive_labels": [],
                    },
                    "risk": "low",
                },
                "replaces_task_id": None,
            }
        ],
        "coverage": [
            {
                "goal_criterion_id": criterion["id"],
                "task_id": task_id,
                "task_acceptance_id": "A1",
                "verification_profile_id": criterion["verification_profile_id"],
            }
        ],
    }


def test_plan_outcome_publishes_running_goal(api, objects):
    client, token, auth, goal, plan_activity = _start_goal(api, objects)
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
    assert lease["activity"]["id"] == plan_activity["id"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    body = {
        "lease": lease["lease"],
        "expected_state_revision": lease["activity"]["state_revision"],
        "outcome": {"plan": _plan_body(goal, profile_id)},
    }
    bad = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            **body,
            "outcome": {
                "plan": {
                    **body["outcome"]["plan"],
                    "coverage": [],
                }
            },
        },
        headers=worker_auth,
    )
    assert bad.status_code == 422
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json=body,
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    activity = done.json()["data"]
    assert activity["status"] == "SUCCEEDED"
    assert activity["state_revision"] == lease["activity"]["state_revision"] + 1
    got_goal = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got_goal["status"] == "RUNNING"
    assert got_goal["plan_revision"] == 1
    assert got_goal["previous_status"] == "PLANNING"
    plans = client.get(f"/api/v1/goals/{goal['id']}/plans", headers=auth)
    assert plans.status_code == 200
    assert len(plans.json()["data"]) == 1
    assert plans.json()["data"][0]["status"] == "PUBLISHED"
    assert plans.json()["data"][0]["plan_revision"] == 1
    tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks", headers=auth).json()["data"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "READY"
    assert tasks[0]["plan_revision"] == 1
    detail = client.get(f"/api/v1/tasks/{tasks[0]['id']}", headers=auth)
    assert detail.status_code == 200
    executes = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "EXECUTE"},
        headers=auth,
    ).json()["data"]
    assert len(executes) == 1
    assert executes[0]["status"] == "READY"
    assert executes[0]["task_id"] == tasks[0]["id"]

    snapshot = client.get(f"/api/v1/goals/{goal['id']}/snapshot", headers=auth)
    assert snapshot.status_code == 200, snapshot.text
    snapshot_data = snapshot.json()["data"]
    assert snapshot_data["plan"]["plan_revision"] == 1
    assert [item["id"] for item in snapshot_data["tasks"]] == [tasks[0]["id"]]
    assert [item["id"] for item in snapshot_data["activities"]] == [executes[0]["id"]]
