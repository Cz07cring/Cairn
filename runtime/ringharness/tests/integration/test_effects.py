"""EXECUTE steps / effect prepare；PLAN 工具路径 403。"""

import hashlib
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from test_claims import _drain_ready, _register_worker, _start_goal
from test_plans import _plan_body


def _publish_and_claim_execute(api, objects):
    """复用 plan outcome 路径拿到 EXECUTE lease。"""
    client, token, auth, goal, plan_activity = _start_goal(api, objects)
    _drain_ready(client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE"))
    # drain 可能误伤本 Goal 的 PLAN；强制恢复。
    import os

    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        # 先清掉所有其他 READY PLAN，再强制恢复本 Goal 的 PLAN。
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
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == plan_activity["id"], (
        "claim 到了其他 Goal 的残留 PLAN，请检查测试库隔离"
    )
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
    # 计划发布后只允许领取本 Goal 的 EXECUTE，避免共享库残留抢 claim。
    import os

    from sqlalchemy import create_engine, text

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
    exec_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert exec_claim.status_code == 200, exec_claim.text
    exec_lease = exec_claim.json()["data"]
    assert exec_lease["lease"] is not None
    assert exec_lease["activity"]["kind"] == "EXECUTE"
    assert exec_lease["activity"]["goal_id"] == goal["id"]
    return client, token, auth, goal, worker_auth, exec_lease


def test_plan_cannot_register_steps(api, objects):
    client, token, _auth, _goal, _plan = _start_goal(api, objects)
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    lease = claimed.json()["data"]
    denied = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/steps",
        json={
            "lease": lease["lease"],
            "predecessor_step_id": None,
            "purpose": "read",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ROLE_TOOL_FORBIDDEN"


def test_execute_step_and_prepare_effect(api, objects):
    store, _, _ = objects
    client, _token, auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "读取入口文件",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    assert step_data["tool_ref"] == "read_file"
    assert step_data["effect_id"] is None
    assert step_data["logical_step_id"] == step_data["id"]

    # 输入工件（必须与 Activity 同项目）
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    engine = client.app.state.engine
    project_id = UUID(exec_lease["activity"]["project_id"])
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{exec_lease['attempt']['worker_id']}",
    )

    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert effect["status"] == "PREPARED"
    assert effect["replay_class"] == "READ_ONLY"
    assert effect["tool_ref"] == "read_file"

    got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert got.status_code == 200
    assert got.json()["data"]["id"] == effect["id"]

    # 幂等重传
    again = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert again.status_code == 201
    assert again.json()["data"]["id"] == effect["id"]

    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["data"]["status"] == "DISPATCHED"
    rev = dispatched.json()["data"]["state_revision"]

    from datetime import UTC, datetime

    receipt_id = str(uuid4())
    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": receipt_id,
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": exec_lease["lease"]["attempt_id"],
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["data"]["disposition"] == "APPLIED"
    final = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert final["status"] == "SUCCEEDED"
    assert final["state_revision"] == rev + 1


def test_incomplete_step_may_be_superseded_by_new_tool_intent(api, objects):
    """未绑定 effect 的占位步骤可被换工具覆盖（≠ 已有 effect 的 STEP_CONFLICT）。"""
    store, _, _ = objects
    client, _token, _auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    first = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "tool:read_file:call-1",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert first.status_code == 201, first.text
    first_data = first.json()["data"]
    assert first_data["effect_id"] is None

    conflict = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "tool:write_file:call-2",
            "tool_ref": "write_file",
        },
        headers=worker_auth,
    )
    assert conflict.status_code == 201, conflict.text
    superseded = conflict.json()["data"]
    assert superseded["id"] == first_data["id"]
    assert superseded["tool_ref"] == "write_file"
    assert superseded["purpose"] == "tool:write_file:call-2"
    assert superseded["intent_revision"] == 1
    assert superseded["effect_id"] is None


def test_prepare_rejects_path_outside_policy(api, objects):
    """ToolCapabilityManifest：越域 path 在 prepare 失败关闭。"""
    store, _, _ = objects
    client, _token, _auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "越域读",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    blob = b'{"path":"etc/passwd"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    engine = client.app.state.engine
    project_id = UUID(exec_lease["activity"]["project_id"])
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{exec_lease['attempt']['worker_id']}",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 422, prepared.text
    err = prepared.json()["error"]
    assert err["code"] == "TOOL_PATH_DENIED"
    assert "TOOL_PATH_DENIED" in err["message"]


def test_effect_receipt_rejects_non_owner_worker(api, objects):
    """Issue #17：非 attempt 持有者不得伪造 Effect 回执（零副作用）。"""
    store, _, _ = objects
    client, token, auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "读取入口文件",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    engine = client.app.state.engine
    project_id = UUID(exec_lease["activity"]["project_id"])
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{exec_lease['attempt']['worker_id']}",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text

    impostor = str(uuid4())
    _register_worker(impostor, kinds=("EXECUTE",))
    impostor_auth = {"Authorization": "Bearer " + token(impostor, ["worker"])}
    from datetime import UTC, datetime

    forged = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": exec_lease["lease"]["attempt_id"],
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=impostor_auth,
    )
    assert forged.status_code == 403, forged.text
    final = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert final["status"] == "DISPATCHED"
    import os

    from sqlalchemy import create_engine, text

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        n = db.execute(
            text("SELECT count(*) FROM effect_receipts WHERE effect_id=:id"),
            {"id": effect["id"]},
        ).scalar_one()
    eng.dispose()
    assert n == 0


def test_effect_receipt_rejects_previous_owner_after_rebind(api, objects):
    """Issue #17 残差：AB02 改绑后，旧 attempt 持有者不得再向该 effect 落回执。

    权威 owner = effect.producer_attempt_id（改绑后为后继 attempt）。
    """
    import os
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine, text

    store, _, _ = objects
    client, token, auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_a = exec_lease["lease"]["attempt_id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    blob = b'{"path":"src/owner-rebind.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:effect-owner-rebind",
    )
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "effect-owner-rebind",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    logical_step_id = step.json()["data"]["logical_step_id"]
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": logical_step_id,
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect_id = prepared.json()["data"]["id"]

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activity_attempts
                  SET lease_expires_at=:past, updated_at=clock_timestamp()
                  WHERE id=:id"""
            ),
            {"id": attempt_a, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )
    probe = str(uuid4())
    _register_worker(probe, kinds=("PLAN", "EXECUTE"))
    client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(probe, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )

    other = str(uuid4())
    _register_worker(other, kinds=("EXECUTE",))
    other_auth = {"Authorization": "Bearer " + token(other, ["worker"])}
    reclaimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**other_auth, "Idempotency-Key": str(uuid4())},
    )
    assert reclaimed.status_code == 200, reclaimed.text
    lease_b = reclaimed.json()["data"]
    attempt_b = lease_b["lease"]["attempt_id"]
    assert attempt_b != attempt_a

    again = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": lease_b["lease"],
            "logical_step_id": logical_step_id,
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=other_auth,
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["id"] == effect_id

    # 旧 holder 仍持有 attempt_a，但 effect 已改绑 attempt_b → 须 403 零写入
    forged = client.post(
        f"/internal/v1/effects/{effect_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect_id,
            "producer_activity_id": activity_id,
            "producer_attempt_id": attempt_a,
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert forged.status_code == 403, forged.text

    with engine.begin() as db:
        n = db.execute(
            text("SELECT count(*) FROM effect_receipts WHERE effect_id=:id"),
            {"id": effect_id},
        ).scalar_one()
        status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": effect_id},
        ).scalar_one()
    engine.dispose()
    assert n == 0
    assert status == "PREPARED"
    # 业务 Goal 不得因本攻击窗口变 DONE
    goal_got = client.get(f"/api/v1/goals/{_goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"


def test_effect_receipt_owner_mismatched_body_pending(api, objects):
    """正确 effect owner 提交参数矛盾回执 → PENDING_RECONCILIATION（落库对账）。"""
    from datetime import UTC, datetime

    store, _, _ = objects
    client, _token, auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "owner-mismatch-pending",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    blob = b'{"path":"src/mismatch.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:mismatch",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text

    # 正确 owner + 矛盾 fencing → PENDING（不推进 DISPATCHED）；不得信错误 attempt 行
    pending = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": attempt_id,
            "fencing_epoch": str(int(exec_lease["lease"]["fencing_epoch"]) + 99),
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert pending.status_code == 201, pending.text
    assert pending.json()["data"]["disposition"] == "PENDING_RECONCILIATION"
    final = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert final["status"] == "DISPATCHED"


def test_create_step_and_prepare_reject_when_goal_paused(api, objects):
    """第136批：Goal PAUSED 时禁止 create_step / prepare 新 ENGINEERING 写入。"""
    import os

    from sqlalchemy import create_engine, text

    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]

    # 先建一步（Goal 仍 RUNNING），再 PAUSED 拦 prepare；另测 create_step 直拦
    step_ok = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "then-pause-before-prepare",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step_ok.status_code == 201, step_ok.text
    step_data = step_ok.json()["data"]
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    project_id = UUID(exec_lease["activity"]["project_id"])
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{exec_lease['attempt']['worker_id']}",
    )

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET status='PAUSED', previous_status='RUNNING',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": goal["id"]},
        )
    eng.dispose()

    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 422, prepared.text
    prep_err = prepared.json()["error"]
    assert prep_err["code"] == "GOAL_ENGINEERING_CLOSED"
    assert "PAUSED" in prep_err["message"]

    step_blocked = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": step_data["id"],
            "purpose": "paused-goal-should-block",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step_blocked.status_code == 422, step_blocked.text
    err = step_blocked.json()["error"]
    assert err["code"] == "GOAL_ENGINEERING_CLOSED"
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] == "PAUSED"
