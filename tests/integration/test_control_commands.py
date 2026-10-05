"""Goal pause / resume / cancel。"""

import os
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _start_goal
from test_replan import _publish_running


def _stops_for_attempt(attempt_id: str):
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        rows = (
            db.execute(
                text(
                    """SELECT status, reason, attempt_id, activation_id
                    FROM stops WHERE attempt_id=:id ORDER BY created_at"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .all()
        )
    engine.dispose()
    return [dict(r) for r in rows]


def _stops_for_goal(goal_id: str):
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        rows = (
            db.execute(
                text(
                    """SELECT s.status, s.reason, s.attempt_id
                    FROM stops s
                    JOIN activities a ON a.id=s.activity_id
                    WHERE a.goal_id=:goal"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .all()
        )
    engine.dispose()
    return [dict(r) for r in rows]


def test_pause_resume_planning_goal(api, objects):
    client, token, auth, goal, _plan = _start_goal(api, objects)
    paused = client.post(
        f"/api/v1/goals/{goal['id']}/pause",
        json={"expected_state_revision": 2, "reason": "operator pause"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert paused.status_code == 202, paused.text
    command = paused.json()["data"]
    assert command["kind"] == "PAUSE"
    assert command["result"]["final_status"] == "PAUSED"
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "PAUSED"
    assert got["previous_status"] == "PLANNING"
    plans = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN", "status": "READY"},
        headers=auth,
    ).json()["data"]
    assert plans == []
    # 清洁 pause（无 RUNNING）：不产生 StopRequest
    assert _stops_for_goal(goal["id"]) == []

    resumed = client.post(
        f"/api/v1/goals/{goal['id']}/resume",
        json={"expected_state_revision": got["state_revision"], "reason": "continue"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["data"]["result"]["final_status"] == "PLANNING"
    after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert after["status"] == "PLANNING"
    _drain_ready_plans(client, token)


def test_pause_running_cancels_ready_execute(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    paused = client.post(
        f"/api/v1/goals/{goal['id']}/pause",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "pause running",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert paused.status_code == 202, paused.text
    assert paused.json()["data"]["result"]["final_status"] == "PAUSED"
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "PAUSED"
    assert got["previous_status"] == "RUNNING"
    executes = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "EXECUTE", "status": "READY"},
        headers=auth,
    ).json()["data"]
    assert executes == []
    # 无 RUNNING attempt：不 emit stop
    assert _stops_for_goal(goal["id"]) == []
    _drain_ready_plans(client, token)


def test_pause_running_emits_stop(api, objects):
    """PAUSING 时对 RUNNING+ACTIVE attempt 发出 reason=PAUSE 的 StopRequest。"""
    client, token, auth, goal = _publish_running(api, objects)
    from test_claims import _register_worker

    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    attempt_id = lease["attempt"]["id"]

    pausing = client.post(
        f"/api/v1/goals/{goal['id']}/pause",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "drain running",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert pausing.status_code == 202, pausing.text
    assert pausing.json()["data"]["result"]["final_status"] == "PAUSING"
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "PAUSING"

    stops = _stops_for_attempt(attempt_id)
    assert len(stops) == 1
    assert stops[0]["status"] == "REQUESTED"
    assert stops[0]["reason"] == "PAUSE"
    assert str(stops[0]["activation_id"]) == attempt_id
    _drain_ready_plans(client, token)


def test_cancel_goal_from_planning(api, objects):
    client, token, auth, goal, _plan = _start_goal(api, objects)
    cancelled = client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={"expected_state_revision": 2, "reason": "abort"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["data"]["kind"] == "CANCEL_GOAL"
    assert cancelled.json()["data"]["result"]["final_status"] == "CANCELLED"
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "CANCELLED"
    again = client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={"expected_state_revision": got["state_revision"], "reason": "again"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert again.status_code == 409
    _drain_ready_plans(client, token)


def test_cancel_stays_cancelling_while_running(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    from test_claims import _register_worker

    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    attempt_id = claimed.json()["data"]["attempt"]["id"]
    cancelling = client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "running work",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert cancelling.status_code == 202, cancelling.text
    assert cancelling.json()["data"]["result"]["final_status"] == "CANCELLING"
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "CANCELLING"

    stops = _stops_for_attempt(attempt_id)
    assert len(stops) == 1
    assert stops[0]["status"] == "REQUESTED"
    assert stops[0]["reason"] == "CANCEL"
    assert str(stops[0]["activation_id"]) == attempt_id
    _drain_ready_plans(client, token)


def test_pause_stays_pausing_until_pause_stop_confirmed(api, objects):
    """活动已排空但 PAUSE Stop 未 CONFIRMED → 保持 PAUSING；确认后才 PAUSED。"""
    from datetime import UTC, datetime
    from uuid import UUID

    from control_kernel.storage.control_commands import maybe_complete_pause_or_cancel

    client, token, auth, goal = _publish_running(api, objects)
    from test_claims import _register_worker

    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    attempt_id = lease["attempt"]["id"]
    activity_id = lease["activity"]["id"]

    pausing = client.post(
        f"/api/v1/goals/{goal['id']}/pause",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "drain then wait stop",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert pausing.status_code == 202, pausing.text
    assert pausing.json()["data"]["result"]["final_status"] == "PAUSING"

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        stop_id = db.execute(
            text("SELECT id FROM stops WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar_one()
        # 模拟宿主已退出活动（排空），但不伪造 Stop CONFIRMED
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": activity_id},
        )
        db.execute(
            text(
                """UPDATE activity_attempts SET status='CANCELLED',
                  updated_at=clock_timestamp() WHERE id=:id"""
            ),
            {"id": attempt_id},
        )
        maybe_complete_pause_or_cancel(db, UUID(goal["id"]))

    still = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert still["status"] == "PAUSING"

    confirm = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop_id),
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert confirm.status_code == 201, confirm.text

    paused = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert paused["status"] == "PAUSED"
    assert paused["previous_status"] == "RUNNING"
    _drain_ready_plans(client, token)


def test_cancel_stays_cancelling_until_cancel_stop_confirmed(api, objects):
    """活动已排空但 CANCEL Stop 未 CONFIRMED → 保持 CANCELLING；确认后才 CANCELLED。"""
    from datetime import UTC, datetime
    from uuid import UUID

    from control_kernel.storage.control_commands import maybe_complete_pause_or_cancel

    client, token, auth, goal = _publish_running(api, objects)
    from test_claims import _register_worker

    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    attempt_id = lease["attempt"]["id"]
    activity_id = lease["activity"]["id"]

    cancelling = client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "drain then wait stop",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert cancelling.status_code == 202, cancelling.text
    assert cancelling.json()["data"]["result"]["final_status"] == "CANCELLING"

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        stop_id = db.execute(
            text("SELECT id FROM stops WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar_one()
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": activity_id},
        )
        db.execute(
            text(
                """UPDATE activity_attempts SET status='CANCELLED',
                  updated_at=clock_timestamp() WHERE id=:id"""
            ),
            {"id": attempt_id},
        )
        maybe_complete_pause_or_cancel(db, UUID(goal["id"]))

    still = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert still["status"] == "CANCELLING"

    confirm = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop_id),
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert confirm.status_code == 201, confirm.text

    cancelled = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert cancelled["status"] == "CANCELLED"
    _drain_ready_plans(client, token)


def test_pause_stop_confirmed_parks_running_without_sql(api, objects):
    """第183批：EXITED 确认后自动 WAITING(PAUSED)→Goal PAUSED；无需手改活动。≠ DONE。"""
    from datetime import UTC, datetime

    client, token, auth, goal = _publish_running(api, objects)
    from test_claims import _register_worker

    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    attempt_id = lease["attempt"]["id"]
    activity_id = lease["activity"]["id"]

    pausing = client.post(
        f"/api/v1/goals/{goal['id']}/pause",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "park via stop receipt",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert pausing.status_code == 202, pausing.text
    assert pausing.json()["data"]["result"]["final_status"] == "PAUSING"

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        stop_id = str(
            db.execute(
                text("SELECT id FROM stops WHERE attempt_id=:id AND reason='PAUSE'"),
                {"id": attempt_id},
            ).scalar_one()
        )
    engine.dispose()

    # 活动仍 RUNNING：仅靠 Stop EXITED 泊入并完成暂停
    still_run = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()[
        "data"
    ]
    assert still_run["status"] == "RUNNING"

    confirm = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop_id,
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert confirm.status_code == 201, confirm.text

    parked = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert parked["status"] == "WAITING"
    assert parked["wait_reason"] == "PAUSED"
    assert parked["status"] != "DONE"

    paused = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert paused["status"] == "PAUSED"
    assert paused["previous_status"] == "RUNNING"
    assert paused["status"] != "DONE"
    _drain_ready_plans(client, token)


def test_cancel_stop_confirmed_parks_running_without_sql(api, objects):
    """第185批：CANCEL EXITED 确认后自动 CANCELLED→Goal CANCELLED；无需手改活动。≠ DONE。"""
    from datetime import UTC, datetime

    client, token, auth, goal = _publish_running(api, objects)
    from test_claims import _register_worker

    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    attempt_id = lease["attempt"]["id"]
    activity_id = lease["activity"]["id"]

    cancelling = client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "park via cancel stop receipt",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert cancelling.status_code == 202, cancelling.text
    assert cancelling.json()["data"]["result"]["final_status"] == "CANCELLING"

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        stop_id = str(
            db.execute(
                text("SELECT id FROM stops WHERE attempt_id=:id AND reason='CANCEL'"),
                {"id": attempt_id},
            ).scalar_one()
        )
    engine.dispose()

    still_run = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()[
        "data"
    ]
    assert still_run["status"] == "RUNNING"

    confirm = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop_id,
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert confirm.status_code == 201, confirm.text

    parked = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert parked["status"] == "CANCELLED"
    assert parked["status"] != "DONE"

    cancelled = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["status"] != "DONE"
    _drain_ready_plans(client, token)


def test_resume_wakes_waiting_paused_after_stop_park(api, objects):
    """第184批：Stop 泊入 WAITING(PAUSED) 后 resume 须唤醒为 READY；≠ DONE。"""
    from datetime import UTC, datetime

    client, token, auth, goal = _publish_running(api, objects)
    from test_claims import _register_worker

    subject = str(uuid4())
    _register_worker(subject, kinds=("EXECUTE",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    attempt_id = lease["attempt"]["id"]
    activity_id = lease["activity"]["id"]

    pausing = client.post(
        f"/api/v1/goals/{goal['id']}/pause",
        json={
            "expected_state_revision": goal["state_revision"],
            "reason": "park then resume",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert pausing.status_code == 202, pausing.text

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        stop_id = str(
            db.execute(
                text("SELECT id FROM stops WHERE attempt_id=:id AND reason='PAUSE'"),
                {"id": attempt_id},
            ).scalar_one()
        )
    engine.dispose()

    confirm = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop_id,
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert confirm.status_code == 201, confirm.text
    paused = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert paused["status"] == "PAUSED"
    parked = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert parked["status"] == "WAITING"
    assert parked["wait_reason"] == "PAUSED"

    resumed = client.post(
        f"/api/v1/goals/{goal['id']}/resume",
        json={
            "expected_state_revision": paused["state_revision"],
            "reason": "wake parked execute",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["data"]["result"]["final_status"] == "RUNNING"
    after_goal = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert after_goal["status"] == "RUNNING"
    assert after_goal["status"] != "DONE"

    woken = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert woken["status"] == "READY"
    assert woken["wait_reason"] is None
    assert woken["resume_state"] is None

    # 唤醒后可重新 claim（新 attempt / fencing）；≠ DONE
    other = str(uuid4())
    _register_worker(other, kinds=("EXECUTE",))
    reclaimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(other, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert reclaimed.status_code == 200, reclaimed.text
    new_lease = reclaimed.json()["data"]
    assert new_lease["activity"]["id"] == activity_id
    assert new_lease["lease"] is not None
    assert int(new_lease["lease"]["fencing_epoch"]) > int(lease["lease"]["fencing_epoch"])
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"
    _drain_ready_plans(client, token)
