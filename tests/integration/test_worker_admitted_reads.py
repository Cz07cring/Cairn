"""worker 对已 admit 活动/Goal/Task 的只读：TEMPORAL RunActivation / seal 前置。"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import admit_runtime_attempt
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _drain_ready_plans, _register_worker, _start_goal
from test_orchestration_backend import _start_temporal_goal
from test_plans import _plan_body


def test_worker_reads_admitted_activity_and_goal(api, objects):
    """仅持有 ACTIVE attempt 的 worker 可读 GET activity/goal；无租约 404。"""
    client, token, _auth, goal, plan, _command, engine = _start_temporal_goal(api, objects)
    subject = str(uuid4())
    stranger = str(uuid4())
    _register_worker(subject)
    _register_worker(stranger)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    stranger_auth = {"Authorization": "Bearer " + token(stranger, ["worker"])}

    try:
        # 未 admit：worker 不可读
        before = client.get(f"/api/v1/activities/{plan['id']}", headers=worker_auth)
        assert before.status_code == 404, before.text

        lease = admit_runtime_attempt(
            engine,
            subject,
            f"test-admit-read:{plan['id']}",
            UUID(plan["id"]),
        )
        assert lease.lease is not None

        got_act = client.get(f"/api/v1/activities/{plan['id']}", headers=worker_auth)
        assert got_act.status_code == 200, got_act.text
        assert got_act.json()["data"]["id"] == plan["id"]

        got_goal = client.get(f"/api/v1/goals/{goal['id']}", headers=worker_auth)
        assert got_goal.status_code == 200, got_goal.text
        assert got_goal.json()["data"]["id"] == goal["id"]
        assert "contract" in got_goal.json()["data"]

        # 其他 worker 无 attempt → 404
        other = client.get(f"/api/v1/activities/{plan['id']}", headers=stranger_auth)
        assert other.status_code == 404, other.text
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()


def test_worker_reads_admitted_execute_task_acceptance(api, objects):
    """EXECUTE 租约下 worker 可读 Task acceptance（seal MECHANICAL 源）；无租约 404。"""
    client, token, _auth, goal, plan_activity = _start_goal(api, objects)
    _drain_ready(client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE"))
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
              WHERE status='READY' AND kind='PLAN' AND id<>:id"""),
            {"id": plan_activity["id"]},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('CANCELLED','READY')"""
            ),
            {"id": plan_activity["id"]},
        )
    engine.dispose()

    subject = str(uuid4())
    stranger = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    _register_worker(stranger, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    stranger_auth = {"Authorization": "Bearer " + token(stranger, ["worker"])}

    claimed_plan = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed_plan.status_code == 200, claimed_plan.text
    plan_lease = claimed_plan.json()["data"]
    assert plan_lease["activity"]["id"] == plan_activity["id"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    outcome = client.post(
        f"/internal/v1/activities/{plan_lease['activity']['id']}/outcomes",
        json={
            "lease": plan_lease["lease"],
            "expected_state_revision": plan_lease["activity"]["state_revision"],
            "outcome": {"plan": _plan_body(goal, profile_id)},
        },
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert outcome.status_code == 200, outcome.text

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    engine.dispose()

    claimed_exec = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed_exec.status_code == 200, claimed_exec.text
    exec_lease = claimed_exec.json()["data"]
    task_id = exec_lease["activity"]["task_id"]
    assert task_id, "EXECUTE 须绑定 task_id"

    got_task = client.get(f"/api/v1/tasks/{task_id}", headers=worker_auth)
    assert got_task.status_code == 200, got_task.text
    assert got_task.json()["data"]["id"] == task_id
    acceptance = got_task.json()["data"]["contract"]["acceptance"]
    assert acceptance
    assert acceptance[0]["verification_profile_id"] == profile_id

    denied = client.get(f"/api/v1/tasks/{task_id}", headers=stranger_auth)
    assert denied.status_code == 404, denied.text


def test_worker_reads_goal_audits_when_admitted(api, objects):
    """第278批：持 ACTIVE attempt 的 worker 可读 Goal audits（INTEGRATE aggregation）；无租约 404。"""
    client, token, _auth, goal, plan, _command, engine = _start_temporal_goal(api, objects)
    subject = str(uuid4())
    stranger = str(uuid4())
    _register_worker(subject)
    _register_worker(stranger)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    stranger_auth = {"Authorization": "Bearer " + token(stranger, ["worker"])}

    try:
        before = client.get(f"/api/v1/goals/{goal['id']}/audits", headers=worker_auth)
        assert before.status_code == 404, before.text

        lease = admit_runtime_attempt(
            engine,
            subject,
            f"test-admit-audits:{plan['id']}",
            UUID(plan["id"]),
        )
        assert lease.lease is not None

        got = client.get(f"/api/v1/goals/{goal['id']}/audits?limit=20", headers=worker_auth)
        assert got.status_code == 200, got.text
        body = got.json()["data"]
        assert isinstance(body, list)
        # 尚无候选审计亦可空列表；关键是门禁放行
        other = client.get(f"/api/v1/goals/{goal['id']}/audits", headers=stranger_auth)
        assert other.status_code == 404, other.text
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
