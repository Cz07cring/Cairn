"""Goal replan：RUNNING→PLANNING 新建 PLAN；屏障下拒绝。"""

import os
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _register_worker, _start_goal
from test_plans import _plan_body


def _publish_running(api, objects):
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
            "outcome": {"plan": _plan_body(goal, profile_id)},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    running = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert running["status"] == "RUNNING"
    return client, token, auth, running


def test_replan_creates_plan_activity(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    assert (
        client.post(
            f"/api/v1/goals/{goal['id']}/replan",
            json={
                "expected_state_revision": goal["state_revision"],
                "expected_plan_revision": goal["plan_revision"],
                "reason": "viewer cannot",
            },
            headers={
                "Authorization": "Bearer " + token(str(uuid4()), ["viewer"]),
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 403
    )
    stale = client.post(
        f"/api/v1/goals/{goal['id']}/replan",
        json={
            "expected_state_revision": goal["state_revision"],
            "expected_plan_revision": 99,
            "reason": "stale plan",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STATE_REVISION_CONFLICT"
    key = str(uuid4())
    body = {
        "expected_state_revision": goal["state_revision"],
        "expected_plan_revision": goal["plan_revision"],
        "reason": "audit feedback",
    }
    replanned = client.post(
        f"/api/v1/goals/{goal['id']}/replan",
        json=body,
        headers={**auth, "Idempotency-Key": key},
    )
    assert replanned.status_code == 202, replanned.text
    command = replanned.json()["data"]
    assert command["kind"] == "REPLAN"
    assert command["status"] == "SUCCEEDED"
    assert command["result"] == {
        "goal_id": goal["id"],
        "plan_revision": goal["plan_revision"],
    }
    assert (
        client.post(
            f"/api/v1/goals/{goal['id']}/replan",
            json=body,
            headers={**auth, "Idempotency-Key": key},
        ).json()["data"]
        == command
    )
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "PLANNING"
    assert got["previous_status"] == "RUNNING"
    assert got["state_revision"] == goal["state_revision"] + 1
    assert got["plan_revision"] == goal["plan_revision"]
    plans = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN", "status": "READY"},
        headers=auth,
    ).json()["data"]
    assert len(plans) == 1
    assert plans[0]["binding"]["plan_revision"] == goal["plan_revision"]
    executes = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "EXECUTE", "status": "READY"},
        headers=auth,
    ).json()["data"]
    assert executes == []


def test_replan_rejected_under_barrier(api, objects):
    client, _token, auth, goal = _publish_running(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO finalization_barriers(
                  id,project_id,goal_id,write_epoch,status,contract_revision,plan_revision,
                  candidate_manifest_id,in_flight_engineering,unknown_effects)
                VALUES(
                  :id,:project,:goal,:epoch,'DRAINING',:crev,:prev,NULL,0,0)"""
            ),
            {
                "id": uuid4(),
                "project": goal["project_id"],
                "goal": goal["id"],
                "epoch": goal["write_epoch"],
                "crev": goal["contract_revision"],
                "prev": goal["plan_revision"],
            },
        )
    engine.dispose()
    blocked = client.post(
        f"/api/v1/goals/{goal['id']}/replan",
        json={
            "expected_state_revision": goal["state_revision"],
            "expected_plan_revision": goal["plan_revision"],
            "reason": "should fail",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INVALID_STATE"
    # 避免 DRAINING 屏障 + READY EXECUTE 污染后续用例 claim。
    from test_claims import _drain_ready_plans

    _drain_ready_plans(client, _token)
