"""第三百四十四批：activation_terminations 持久化；≠ Goal DONE。"""

from __future__ import annotations

from uuid import UUID, uuid4

from control_kernel.protocols.runtime import PlanRejected
from control_kernel.storage.activation_terminations import (
    list_activation_terminations,
    record_activation_termination,
)
from sqlalchemy import text
from test_claims import _register_worker, _start_goal


def test_record_no_progress_stop_not_done(api, objects):
    """NO_PROGRESS_STOP 落库；Goal 保持非 DONE；幂等同 attempt。"""
    client, token, auth, goal, plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])
    activity_id = UUID(plan["id"])

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code in (200, 201), claimed.text
    lease = claimed.json()["data"]["lease"]
    attempt_id = UUID(lease["attempt_id"])

    before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert before["status"] != "DONE"

    row = record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=attempt_id,
        reason="NO_PROGRESS_STOP",
        detail="nudge_budget_exhausted",
    )
    assert row["marks_goal_done"] is False
    assert row["reason"] == "NO_PROGRESS_STOP"
    assert row["attempt_id"] == attempt_id

    again = record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=attempt_id,
        reason="NO_PROGRESS_STOP",
    )
    assert again["id"] == row["id"]
    assert again["marks_goal_done"] is False

    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["status"] == before["status"]
    assert after["status"] != "DONE"

    with engine.connect() as db:
        goal_status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
    assert goal_status != "DONE"

    listed = list_activation_terminations(
        engine, goal_id, subject=subject, project_ids=[]
    )
    assert len(listed) == 1
    assert listed[0]["reason"] == "NO_PROGRESS_STOP"
    assert listed[0]["marks_goal_done"] is False


def test_record_reason_conflict_and_unknown(api, objects):
    client, token, _auth, _goal, plan = _start_goal(api, objects)
    engine = client.app.state.engine
    activity_id = UUID(plan["id"])

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code in (200, 201), claimed.text
    attempt_id = UUID(claimed.json()["data"]["lease"]["attempt_id"])

    record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=attempt_id,
        reason="CANCELLED",
    )
    try:
        record_activation_termination(
            engine,
            subject=subject,
            project_ids=[],
            activity_id=activity_id,
            attempt_id=attempt_id,
            reason="BUDGET_EXHAUSTED",
        )
        raise AssertionError("expected conflict")
    except PlanRejected as exc:
        assert "CONFLICT" in str(exc)

    try:
        record_activation_termination(
            engine,
            subject=subject,
            project_ids=[],
            activity_id=activity_id,
            attempt_id=attempt_id,
            reason="GOAL_DONE",
        )
        raise AssertionError("expected unknown")
    except PlanRejected as exc:
        assert "未知" in str(exc)


def test_http_activation_termination_owner_only_not_done(api, objects):
    """第三百四十五批：internal HTTP + lease 持有者；非持有者 403；≠ DONE。"""
    from control_kernel.protocols.runtime import LeaseIdentity

    client, token, auth, goal, plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    activity_id = UUID(plan["id"])

    owner = str(uuid4())
    _register_worker(owner, kinds=("PLAN",))
    owner_auth = {"Authorization": "Bearer " + token(owner, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**owner_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code in (200, 201), claimed.text
    lease_body = claimed.json()["data"]["lease"]

    stranger = str(uuid4())
    _register_worker(stranger, kinds=("PLAN", "EXECUTE"))
    stranger_auth = {"Authorization": "Bearer " + token(stranger, ["worker"])}
    deny = client.post(
        f"/internal/v1/activities/{activity_id}/activation-terminations",
        json={
            "lease": lease_body,
            "reason": "NO_PROGRESS_STOP",
            "detail": "stranger",
        },
        headers={**stranger_auth, "Idempotency-Key": str(uuid4())},
    )
    assert deny.status_code == 403, deny.text

    ok = client.post(
        f"/internal/v1/activities/{activity_id}/activation-terminations",
        json={
            "lease": lease_body,
            "reason": "NO_PROGRESS_STOP",
            "detail": "from_http",
        },
        headers={**owner_auth, "Idempotency-Key": str(uuid4())},
    )
    assert ok.status_code == 201, ok.text
    data = ok.json()["data"]
    assert data["marks_goal_done"] is False
    assert data["reason"] == "NO_PROGRESS_STOP"

    again = client.post(
        f"/internal/v1/activities/{activity_id}/activation-terminations",
        json={"lease": lease_body, "reason": "NO_PROGRESS_STOP"},
        headers={**owner_auth, "Idempotency-Key": str(uuid4())},
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["id"] == data["id"]

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] != "DONE"
    LeaseIdentity.model_validate(lease_body)

    listed = client.get(
        f"/api/v1/goals/{goal_id}/activation-terminations",
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert len(rows) == 1
    assert rows[0]["id"] == data["id"]
    assert rows[0]["reason"] == "NO_PROGRESS_STOP"
    assert rows[0]["marks_goal_done"] is False
    assert got["status"] != "DONE"
