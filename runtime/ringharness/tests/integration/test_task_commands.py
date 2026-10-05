"""Task cancel / retry。"""

import os
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _register_worker
from test_replan import _publish_running


def test_cancel_ready_task(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks", headers=auth).json()["data"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["status"] == "READY"
    # status 服务端筛选（doc/05）：READY 命中，DONE 为空
    ready_only = client.get(
        f"/api/v1/goals/{goal['id']}/tasks",
        params={"status": "READY"},
        headers=auth,
    )
    assert ready_only.status_code == 200, ready_only.text
    assert [t["id"] for t in ready_only.json()["data"]] == [task["id"]]
    done_only = client.get(
        f"/api/v1/goals/{goal['id']}/tasks",
        params={"status": "DONE"},
        headers=auth,
    )
    assert done_only.status_code == 200, done_only.text
    assert done_only.json()["data"] == []
    cancelled = client.post(
        f"/api/v1/tasks/{task['id']}/cancel",
        json={"expected_state_revision": task["state_revision"], "reason": "stop task"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert cancelled.status_code == 202, cancelled.text
    command = cancelled.json()["data"]
    assert command["kind"] == "CANCEL_TASK"
    assert command["result"] == {"task_id": task["id"], "final_status": "CANCELLED"}
    got = client.get(f"/api/v1/tasks/{task['id']}", headers=auth).json()["data"]
    assert got["status"] == "CANCELLED"
    executes = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "EXECUTE", "status": "READY"},
        headers=auth,
    ).json()["data"]
    assert executes == []
    _drain_ready_plans(client, token)


def test_cancel_stays_cancelling_while_execute_running(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks", headers=auth).json()["data"]
    task = tasks[0]
    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["activity"]["task_id"] == task["id"]
    task = client.get(f"/api/v1/tasks/{task['id']}", headers=auth).json()["data"]
    assert task["status"] == "RUNNING"
    cancelling = client.post(
        f"/api/v1/tasks/{task['id']}/cancel",
        json={"expected_state_revision": task["state_revision"], "reason": "drain"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert cancelling.status_code == 202, cancelling.text
    assert cancelling.json()["data"]["result"]["final_status"] == "CANCELLING"
    got = client.get(f"/api/v1/tasks/{task['id']}", headers=auth).json()["data"]
    assert got["status"] == "BLOCKED"
    assert got["block_reason"] == "CANCELLING"
    _drain_ready_plans(client, token)


def test_retry_cancelled_task_creates_replacement(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks", headers=auth).json()["data"]
    task = tasks[0]
    cancelled = client.post(
        f"/api/v1/tasks/{task['id']}/cancel",
        json={"expected_state_revision": task["state_revision"], "reason": "will retry"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert cancelled.status_code == 202, cancelled.text
    task = client.get(f"/api/v1/tasks/{task['id']}", headers=auth).json()["data"]
    retried = client.post(
        f"/api/v1/tasks/{task['id']}/retry",
        json={"expected_state_revision": task["state_revision"], "reason": "try again"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert retried.status_code == 202, retried.text
    command = retried.json()["data"]
    assert command["kind"] == "RETRY_TASK"
    replacement_id = command["result"]["replacement_task_id"]
    assert command["result"]["work_lineage_id"] == task["work_lineage_id"]
    replacement = client.get(f"/api/v1/tasks/{replacement_id}", headers=auth).json()["data"]
    assert replacement["status"] == "READY"
    assert replacement["replaces_task_id"] == task["id"]
    assert replacement["work_lineage_id"] == task["work_lineage_id"]
    assert replacement["execution_round"] == task["execution_round"] + 1
    executes = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "EXECUTE", "status": "READY"},
        headers=auth,
    ).json()["data"]
    assert len(executes) == 1
    assert executes[0]["task_id"] == replacement_id
    _drain_ready_plans(client, token)


def test_retry_rejects_when_rounds_exhausted(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks", headers=auth).json()["data"]
    task = tasks[0]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE tasks SET status='FAILED',
                  execution_round=:round, state_revision=state_revision+1,
                  updated_at=clock_timestamp()
                  WHERE id=:id"""
            ),
            {
                "id": task["id"],
                "round": task["contract"]["retry_policy"]["max_execution_rounds"],
            },
        )
    engine.dispose()
    failed = client.get(f"/api/v1/tasks/{task['id']}", headers=auth).json()["data"]
    bad = client.post(
        f"/api/v1/tasks/{failed['id']}/retry",
        json={"expected_state_revision": failed["state_revision"], "reason": "no rounds"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert bad.status_code == 409
    _drain_ready_plans(client, token)
