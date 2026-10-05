"""Claim READY PLAN Activity 与 heartbeat；无 outcome / 工具。"""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_goals import _ready_project


def _register_worker(subject: str, kinds=("PLAN",), *, cpu=1000, memory=2**30, disk=2**30, model=4):
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    worker_id = uuid4()
    with engine.begin() as db:
        db.execute(
            text("""INSERT INTO workers(
              id,subject,allowed_kinds,capabilities,cpu_millicores,memory_bytes,disk_bytes,
              model_slots,browser_slots,status)
            VALUES(
              :id,:subject,:kinds,'{}',:cpu,:memory,:disk,:model,0,'ACTIVE')"""),
            {
                "id": worker_id,
                "subject": subject,
                "kinds": list(kinds),
                "cpu": cpu,
                "memory": memory,
                "disk": disk,
                "model": model,
            },
        )
    engine.dispose()
    return str(worker_id)


def _drain_ready(client, token, kinds=("PLAN",)):
    """排空共享测试库中残留 READY/RUNNING 活动，避免跨用例串扰。"""
    import os

    from sqlalchemy import create_engine, text

    # 对 BINDING_STALE / 脏数据直接取消 READY，避免 claim 死循环。
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
              WHERE status IN ('READY','RUNNING','RECOVERING') AND kind = ANY(:kinds)"""),
            {"kinds": list(kinds)},
        )
        # 过期仍挂 ACTIVE 的 attempt 一并收尾，防止失租扫描把旧活动弄回 READY。
        db.execute(
            text(
                """UPDATE activity_attempts SET status='CANCELLED',
                  finished_at=clock_timestamp(), updated_at=clock_timestamp()
                WHERE status='ACTIVE'
                  AND activity_id IN (
                    SELECT id FROM activities WHERE kind = ANY(:kinds)
                  )"""
            ),
            {"kinds": list(kinds)},
        )
    engine.dispose()


def _drain_ready_plans(client, token):
    _drain_ready(
        client,
        token,
        kinds=(
            "PLAN",
            "EXECUTE",
            "AUDIT",
            "FINALIZE",
            "INTEGRATE",
            "RECONCILE",
            "VALIDATE_SKILL",
            "PROBE_MODEL",
            "EXPORT_EVIDENCE",
            "INDEX_MEMORY",
        ),
    )
    import os

    from sqlalchemy import create_engine, text

    # 共享库上未收尾的 RUNNING PLAN 会在租约恢复后变 READY，抢后续 claim。
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE kind='PLAN' AND status='RUNNING'"""
            )
        )
    engine.dispose()


def _start_goal(api, objects):
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    _drain_ready_plans(client, token)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    plan = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    ).json()["data"][0]
    return client, token, auth, goal, plan


def test_claim_plan_and_heartbeat(api, objects):
    client, token, auth, goal, plan = _start_goal(api, objects)
    subject = str(uuid4())
    worker_id = _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}

    stranger = str(uuid4())
    _register_worker(stranger, kinds=("EXECUTE",))
    assert (
        client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={
                "Authorization": "Bearer " + token(stranger, ["worker"]),
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={
                "Authorization": "Bearer " + token(str(uuid4()), ["worker"]),
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 403
    )

    key = str(uuid4())
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": key},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["lease"] is not None
    assert lease["activity"]["id"] == plan["id"]
    assert lease["activity"]["kind"] == "PLAN"
    assert lease["activity"]["status"] == "RUNNING"
    assert lease["activity"]["goal_id"] == goal["id"]
    assert lease["activity"]["current_attempt_id"] == lease["attempt"]["id"]
    assert lease["attempt"]["worker_id"] == worker_id
    assert lease["attempt"]["status"] == "ACTIVE"
    assert lease["attempt"]["renewal_seq"] == 0
    assert lease["attempt"]["fencing_epoch"] == "1"
    assert lease["input_artifact_ids"] == []
    assert (
        client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={**worker_auth, "Idempotency-Key": key},
        ).json()["data"]
        == lease
    )

    activities = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        headers=auth,
    )
    assert activities.status_code == 200
    assert activities.json()["data"][0]["status"] == "RUNNING"
    assert activities.json()["data"][0]["state_revision"] == 2

    beat = client.post(
        f"/internal/v1/activities/{lease['lease']['activity_id']}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["data"]["renewal_seq"] == 1
    assert beat.json()["data"]["control"] == "CONTINUE"
    replay = client.post(
        f"/internal/v1/activities/{lease['lease']['activity_id']}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["renewal_seq"] == 1
    assert (
        client.post(
            f"/internal/v1/activities/{lease['lease']['activity_id']}/heartbeat",
            json={
                "lease": {**lease["lease"], "fencing_epoch": "99"},
                "renewal_seq": 2,
            },
            headers=worker_auth,
        ).json()["error"]["code"]
        == "FENCING_REJECTED"
    )
    after = client.get(f"/api/v1/activities/{lease['activity']['id']}", headers=auth)
    assert after.json()["data"]["state_revision"] == 2

    other = str(uuid4())
    _register_worker(other)
    second = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(other, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert second.status_code == 200
    assert second.json()["data"]["lease"] is None


def test_concurrent_claim_single_winner(api, objects):
    client, token, _auth, goal, plan = _start_goal(api, objects)
    subjects = [str(uuid4()), str(uuid4())]
    for subject in subjects:
        _register_worker(subject)

    def once(subject):
        return client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={
                "Authorization": "Bearer " + token(subject, ["worker"]),
                "Idempotency-Key": str(uuid4()),
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(once, subjects))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    winners = [r.json()["data"] for r in results if r.json()["data"]["lease"] is not None]
    empties = [r.json()["data"] for r in results if r.json()["data"]["lease"] is None]
    assert len(winners) == 1
    assert len(empties) == 1
    assert winners[0]["activity"]["id"] == plan["id"]
    assert winners[0]["activity"]["goal_id"] == goal["id"]
    assert winners[0]["activity"]["status"] == "RUNNING"


def test_heartbeat_control_stop_when_stop_requested(api, objects):
    """M3：attempt 上有 REQUESTED Stop 时 heartbeat.control=STOP + pending_stop_ids。

    ≠ Goal DONE；不杀进程；Runner 应关闸并走 StopReceipt（本批只证 Kernel 信号）。
    """
    from datetime import UTC, datetime, timedelta

    client, token, auth, _goal, _plan = _start_goal(api, objects)
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
    attempt_id = lease["attempt"]["id"]
    activity_id = lease["lease"]["activity_id"]

    # 无 Stop：CONTINUE + 空 pending
    beat_ok = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert beat_ok.status_code == 200, beat_ok.text
    assert beat_ok.json()["data"]["control"] == "CONTINUE"
    assert beat_ok.json()["data"].get("pending_stop_ids") == []

    stop = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json={
            "request_id": str(uuid4()),
            "activation_id": attempt_id,
            "activity_id": activity_id,
            "attempt_id": attempt_id,
            "fencing_epoch": lease["lease"]["fencing_epoch"],
            "reason": "PAUSE",
            "deadline_at": (datetime.now(UTC) + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        },
        headers=auth,
    )
    assert stop.status_code == 202, stop.text
    stop_id = stop.json()["data"]["id"]
    assert stop.json()["data"]["status"] == "REQUESTED"

    beat_stop = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 2},
        headers=worker_auth,
    )
    assert beat_stop.status_code == 200, beat_stop.text
    body = beat_stop.json()["data"]
    assert body["control"] == "STOP"
    assert body["pending_stop_ids"] == [stop_id]
    assert body["renewal_seq"] == 2

    # 幂等重放 renewal_seq 仍须 STOP（不得因不续约而漏信号）
    replay = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 2},
        headers=worker_auth,
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["control"] == "STOP"
    assert replay.json()["data"]["pending_stop_ids"] == [stop_id]


def test_heartbeat_stop_clears_only_after_exited_confirmed(api, objects):
    """M3：RUNNING 回执不清除 STOP；EXITED 确认后活动泊入 WAITING，Goal→PAUSED。

    排空期（REQUESTED / RUNNING 观察）heartbeat 保持 STOP；确认后 attempt 结束，
    旧租约 heartbeat 失败关闭（不得 CONTINUE 重开工具）。≠ Goal DONE。
    """
    from datetime import UTC, datetime

    client, token, auth, goal, _plan = _start_goal(api, objects)
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
    attempt_id = lease["attempt"]["id"]
    activity_id = lease["lease"]["activity_id"]
    goal_rev = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "state_revision"
    ]

    # 经公开 pause：Goal→PAUSING + PAUSE Stop
    pausing = client.post(
        f"/api/v1/goals/{goal['id']}/pause",
        json={"expected_state_revision": goal_rev, "reason": "heartbeat drain"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert pausing.status_code == 202, pausing.text
    assert pausing.json()["data"]["result"]["final_status"] == "PAUSING"

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        stop_id = str(
            db.execute(
                text(
                    """SELECT id FROM stops
                    WHERE attempt_id=:id AND reason='PAUSE' AND status='REQUESTED'"""
                ),
                {"id": attempt_id},
            ).scalar_one()
        )
    engine.dispose()

    beat_stop = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert beat_stop.status_code == 200, beat_stop.text
    assert beat_stop.json()["data"]["control"] == "STOP"
    assert beat_stop.json()["data"]["pending_stop_ids"] == [stop_id]

    # harness 诚实 RUNNING：Stop 仍 REQUESTED → 仍 STOP
    running = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop_id,
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "RUNNING",
            "compute_released": False,
            "write_capability_revoked": False,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert running.status_code == 201, running.text
    beat_running = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 2},
        headers=worker_auth,
    )
    assert beat_running.json()["data"]["control"] == "STOP"
    assert beat_running.json()["data"]["pending_stop_ids"] == [stop_id]

    # EXITED → 泊入 WAITING(PAUSED) + Goal PAUSED；旧 attempt 心跳失败关闭
    exited = client.post(
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
    assert exited.status_code == 201, exited.text
    confirmed = client.get(f"/internal/v1/stops/{stop_id}", headers=auth).json()["data"]
    assert confirmed["status"] == "CONFIRMED"
    parked = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert parked["status"] == "WAITING"
    assert parked["wait_reason"] == "PAUSED"
    paused = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert paused["status"] == "PAUSED"
    assert paused["status"] != "DONE"

    beat_after = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 3},
        headers=worker_auth,
    )
    assert beat_after.status_code == 409, beat_after.text
    assert beat_after.json()["error"]["code"] in (
        "INVALID_STATE",
        "LEASE_EXPIRED",
        "FENCING_REJECTED",
    )


def test_heartbeat_stop_when_goal_blocked_after_abandon(api, objects):
    """第177/180/186批：放弃→BLOCKED 后 heartbeat STOP，并带 SHUTDOWN pending；≠ DONE。"""
    from uuid import UUID

    from control_kernel.storage.abandonments import record_orchestration_abandonment
    from sqlalchemy import text

    client, token, auth, goal, _plan = _start_goal(api, objects)
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
    activity_id = lease["lease"]["activity_id"]
    attempt_id = lease["attempt"]["id"]

    beat_ok = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert beat_ok.status_code == 200, beat_ok.text
    assert beat_ok.json()["data"]["control"] == "CONTINUE"

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        client.app.state.engine,
        UUID(goal["id"]),
        subject=abandoner,
        project_ids=[],
        reason="INTENT_EXPIRED",
        generation=11,
    )
    blocked = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    with client.app.state.engine.connect() as db:
        stop_id = str(
            db.execute(
                text(
                    """SELECT id FROM stops
                    WHERE attempt_id=:id AND reason='SHUTDOWN'"""
                ),
                {"id": attempt_id},
            ).scalar_one()
        )

    beat_stop = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 2},
        headers=worker_auth,
    )
    assert beat_stop.status_code == 200, beat_stop.text
    body = beat_stop.json()["data"]
    assert body["control"] == "STOP"
    assert body["pending_stop_ids"] == [stop_id]
