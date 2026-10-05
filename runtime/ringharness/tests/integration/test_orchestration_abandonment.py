"""Issue #24：编排放弃恢复必须持久化为业务事实；≠ Goal DONE。"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from control_kernel.protocols.runtime import PlanRejected
from control_kernel.storage.abandonments import record_orchestration_abandonment
from control_kernel.storage.policies import ScopeNotFound
from sqlalchemy import text
from test_claims import _register_worker, _start_goal


def test_record_abandonment_emits_shutdown_stop_for_running(api, objects):
    """第180批：放弃后对 RUNNING+ACTIVE 发出 SHUTDOWN Stop（不杀进程）；≠ DONE。"""
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
    goal_id = UUID(goal["id"])

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        client.app.state.engine,
        goal_id,
        subject=abandoner,
        project_ids=[],
        reason="RECOVERY_DISABLED",
        generation=31,
    )
    blocked = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    with client.app.state.engine.connect() as db:
        stop = (
            db.execute(
                text(
                    """SELECT id, status, reason FROM stops
                    WHERE attempt_id=:id AND reason='SHUTDOWN'"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .one()
        )
    assert stop["status"] == "REQUESTED"

    beat = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert beat.status_code == 200, beat.text
    body = beat.json()["data"]
    assert body["control"] == "STOP"
    assert body["pending_stop_ids"] == [str(stop["id"])]


def test_goal_blocked_rejects_execute_outcome_after_abandon(api, objects):
    """第181批：BLOCKED 后禁止 EXECUTE 成功 outcome 推进 Task；≠ DONE。"""
    import hashlib
    import json
    from datetime import UTC, datetime
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "pre-abandon-read",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:input",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
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
    assert dispatched.status_code == 200
    receipt = client.post(
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
            "result_artifact_ids": [str(input_art.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text

    file_bytes = b"print('ok')\n"
    file_digest = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    Artifacts(engine, store).ingest_raw(
        project_id,
        file_digest,
        BytesIO(file_bytes),
        mime="text/x-python",
        producer_identity="test:candidate-file",
    )
    snapshot = {
        "files": [{"path": "src/main.py", "digest": file_digest, "mode": "100644"}],
        "git_commit": None,
        "dependency_lock_digests": [],
        "submodules": [],
        "lfs_objects": [],
        "image_digests": [],
    }
    snap_raw = json.dumps(snapshot, separators=(",", ":")).encode()
    snap_digest = "sha256:" + hashlib.sha256(snap_raw).hexdigest()
    snap_art = Artifacts(engine, store).ingest_raw(
        project_id,
        snap_digest,
        BytesIO(snap_raw),
        mime="application/json",
        producer_identity="test:snapshot",
    )
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    sealed = client.post(
        "/internal/v1/candidates/seal",
        json={
            "lease": exec_lease["lease"],
            "workspace_snapshot_artifact_id": str(snap_art.id),
            "verification_profile_ids": [profile_id],
        },
        headers=worker_auth,
    )
    assert sealed.status_code == 201, sealed.text
    candidate = sealed.json()["data"]

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        engine,
        goal_id,
        subject=abandoner,
        project_ids=[],
        reason="INTENT_EXPIRED",
        generation=41,
    )
    blocked = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    denied = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": exec_lease["lease"],
            "expected_state_revision": exec_lease["activity"]["state_revision"],
            "outcome": {
                "candidate_manifest_id": candidate["id"],
                "evidence_ids": [str(input_art.id)],
            },
        },
        headers=worker_auth,
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "GOAL_OUTCOME_CLOSED"
    assert "BLOCKED" in denied.json()["error"]["message"]

    task = client.get(
        f"/api/v1/tasks/{exec_lease['activity']['task_id']}", headers=auth
    ).json()["data"]
    assert task["status"] != "VERIFYING"
    act = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert act["status"] == "RUNNING"


def test_shutdown_stop_confirmed_parks_running_without_sql(api, objects):
    """第186批：放弃 SHUTDOWN EXITED 后 Activity→CANCELLED；可解除 BLOCKED；≠ DONE。"""
    from datetime import UTC, datetime

    from control_kernel.storage.abandonments import (
        release_orchestration_abandonment_block,
    )

    client, token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])
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

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        engine,
        goal_id,
        subject=abandoner,
        project_ids=[],
        reason="RECOVERY_DISABLED",
        generation=86,
    )
    blocked = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    with engine.begin() as db:
        stop_id = str(
            db.execute(
                text(
                    """SELECT id FROM stops
                    WHERE attempt_id=:id AND reason='SHUTDOWN'"""
                ),
                {"id": attempt_id},
            ).scalar_one()
        )

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
    # Goal 仍 BLOCKED（放弃事实保留），绝不因 Stop 确认变 DONE
    still_blocked = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert still_blocked["status"] == "BLOCKED"
    assert still_blocked["status"] != "DONE"

    released = release_orchestration_abandonment_block(
        engine,
        goal_id,
        subject=abandoner,
        project_ids=[],
        expected_state_revision=still_blocked["state_revision"],
    )
    assert released["marks_goal_done"] is False
    assert released["status"] != "DONE"
    assert released["status"] == "PLANNING"


def test_list_and_release_orchestration_abandonment_block(api, objects):
    """第182批：放弃记录可读；人工解除 BLOCKED 恢复 previous_status；≠ DONE。"""
    from datetime import UTC, datetime

    from control_kernel.storage.abandonments import (
        list_orchestration_abandonments,
        release_orchestration_abandonment_block,
    )

    client, token, auth, goal, plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])
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

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        engine,
        goal_id,
        subject=abandoner,
        project_ids=[],
        reason="RECOVERY_ATTEMPTS_EXCEEDED",
        generation=50,
        prior_run_id="wf-50",
    )
    rows = list_orchestration_abandonments(
        engine, goal_id, subject=abandoner, project_ids=[]
    )
    assert len(rows) == 1
    assert rows[0]["reason"] == "RECOVERY_ATTEMPTS_EXCEEDED"
    assert rows[0]["marks_goal_done"] is False

    # 未确认 SHUTDOWN → 拒绝解除
    with pytest.raises(PlanRejected, match="STOP_UNCONFIRMED"):
        release_orchestration_abandonment_block(
            engine,
            goal_id,
            subject=abandoner,
            project_ids=[],
            expected_state_revision=client.get(
                f"/api/v1/goals/{goal_id}", headers=auth
            ).json()["data"]["state_revision"],
        )

    # EXITED 确认 Stop 后活动自动 CANCELLED，无需 SQL 手改
    stop_id = None
    with engine.begin() as db:
        stop_id = str(
            db.execute(
                text(
                    """SELECT id FROM stops
                    WHERE attempt_id=:id AND reason='SHUTDOWN'"""
                ),
                {"id": attempt_id},
            ).scalar_one()
        )
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
    parked = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()["data"]
    assert parked["status"] == "CANCELLED"

    rev = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"][
        "state_revision"
    ]
    released = release_orchestration_abandonment_block(
        engine,
        goal_id,
        subject=abandoner,
        project_ids=[],
        expected_state_revision=rev,
    )
    assert released["marks_goal_done"] is False
    assert released["status"] != "DONE"
    assert released["status"] == "PLANNING"  # previous_status before abandon
    assert released["block_reason"] is None

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "PLANNING"
    assert got["status"] != "DONE"
    assert got["block_reason"] is None
    assert plan is not None


def test_record_abandonment_blocks_goal_idempotent_never_done(api, objects):
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])

    worker_subject = str(uuid4())
    _register_worker(worker_subject, kinds=("PLAN", "EXECUTE"))

    first = record_orchestration_abandonment(
        engine,
        goal_id,
        subject=worker_subject,
        project_ids=[],
        reason="INTENT_EXPIRED",
        generation=2,
        prior_run_id="run-abc",
    )
    assert first["marks_goal_done"] is False
    assert first["reason"] == "INTENT_EXPIRED"
    assert first["generation"] == 2

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "BLOCKED"
    assert got["block_reason"] == "ORCHESTRATION_ABANDONED:INTENT_EXPIRED"
    assert got["status"] != "DONE"

    again = record_orchestration_abandonment(
        engine,
        goal_id,
        subject=worker_subject,
        project_ids=[],
        reason="INTENT_EXPIRED",
        generation=2,
        prior_run_id="run-abc",
    )
    assert again["id"] == first["id"]

    with engine.connect() as db:
        count = db.execute(
            text(
                """SELECT count(*) FROM orchestration_abandonments
                WHERE goal_id=:g"""
            ),
            {"g": goal_id},
        ).scalar_one()
        ready = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:g AND status='READY'
                  AND kind = ANY(:kinds)"""
            ),
            {
                "g": goal_id,
                "kinds": ["PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE"],
            },
        ).scalar_one()
    assert count == 1
    assert ready == 0

    with pytest.raises(PlanRejected, match="ORCHESTRATION_ABANDON_CONFLICT"):
        record_orchestration_abandonment(
            engine,
            goal_id,
            subject=worker_subject,
            project_ids=[],
            reason="CHECKPOINT_SCHEMA_INCOMPATIBLE",
            generation=2,
            prior_run_id="run-abc",
        )


def test_record_abandonment_rejects_unknown_worker_and_done(api, objects):
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])

    with pytest.raises(ScopeNotFound):
        record_orchestration_abandonment(
            engine,
            goal_id,
            subject=str(uuid4()),
            project_ids=[],
            reason="INTENT_EXPIRED",
            generation=1,
        )

    worker_subject = str(uuid4())
    _register_worker(worker_subject, kinds=("PLAN",))
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET status='DONE', previous_status='RUNNING',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": goal_id},
        )
    with pytest.raises(PlanRejected, match="终态"):
        record_orchestration_abandonment(
            engine,
            goal_id,
            subject=worker_subject,
            project_ids=[],
            reason="INTENT_EXPIRED",
            generation=1,
        )
    still = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert still["status"] == "DONE"


def test_goal_blocked_rejects_engineering_writes_after_abandon(api, objects):
    """第176批：ORCHESTRATION_ABANDONED→BLOCKED 后 RUNNING 持有者不得新 step/prepare。

    放弃只取消 READY、不杀 RUNNING；若不拦工程咽喉，BLOCKED 可被继续写入。≠ DONE。
    """
    import hashlib
    from io import BytesIO

    from control_kernel.protocols.runtime import PlanRejected
    from control_kernel.storage.artifacts import Artifacts
    from control_kernel.storage.stops import assert_goal_allows_new_engineering_writes
    from sqlalchemy import create_engine
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    goal_id = UUID(goal["id"])

    # 放弃前登记一步 + 输入工件，供 prepare 公开缝
    step_ok = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "pre-abandon-step",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step_ok.status_code == 201, step_ok.text
    step_data = step_ok.json()["data"]
    blob = b'{"path":"src/blocked.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        UUID(exec_lease["activity"]["project_id"]),
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{exec_lease['attempt']['worker_id']}",
    )

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        client.app.state.engine,
        goal_id,
        subject=abandoner,
        project_ids=[],
        reason="RECOVERY_ATTEMPTS_EXCEEDED",
        generation=7,
    )
    blocked = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    # create_step / dispatch 共用的工程写入门必须含 BLOCKED
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db, pytest.raises(
        PlanRejected, match="GOAL_ENGINEERING_CLOSED.*BLOCKED"
    ):
        assert_goal_allows_new_engineering_writes(db, goal_id)
    eng.dispose()

    prep_denied = client.post(
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
    assert prep_denied.status_code == 422, prep_denied.text
    assert prep_denied.json()["error"]["code"] == "GOAL_ENGINEERING_CLOSED"
    assert "BLOCKED" in prep_denied.json()["error"]["message"]


def test_goal_blocked_rejects_new_inference_after_abandon(api, objects):
    """第178批：BLOCKED 后禁止 context-compile / 新 ModelInvocation；≠ DONE。"""
    import hashlib
    import json

    from control_kernel.storage.claims import binding_digest_of

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
    activity_id = lease["activity"]["id"]

    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": compiled.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        client.app.state.engine,
        UUID(goal["id"]),
        subject=abandoner,
        project_ids=[],
        reason="CHECKPOINT_SCHEMA_INCOMPATIBLE",
        generation=19,
    )
    blocked = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    recompile = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert recompile.status_code == 422, recompile.text
    assert recompile.json()["error"]["code"] == "GOAL_INFERENCE_CLOSED"
    assert "BLOCKED" in recompile.json()["error"]["message"]

    prompt = json.dumps(
        {"role": "PLANNER", "tools": [], "instruction": "blocked"},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()
    created = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 256,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "GOAL_INFERENCE_CLOSED"
    assert "BLOCKED" in created.json()["error"]["message"]


def test_goal_blocked_rejects_model_dispatch_after_abandon(api, objects):
    """第179批：AUTHORIZED 后 Goal→BLOCKED，不得 dispatch 外呼；≠ DONE。"""
    import hashlib
    import json

    from control_kernel.storage.claims import binding_digest_of

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
    activity_id = lease["activity"]["id"]

    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": compiled.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]
    prompt = json.dumps(
        {"role": "PLANNER", "tools": [], "instruction": "pre-abandon"},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()
    created = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 64,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert created.status_code == 201, created.text
    inv = created.json()["data"]
    assert inv["status"] == "AUTHORIZED"

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        client.app.state.engine,
        UUID(goal["id"]),
        subject=abandoner,
        project_ids=[],
        reason="WALL_CLOCK_BUDGET_EXHAUSTED",
        generation=21,
    )
    blocked = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    denied = client.post(
        f"/internal/v1/model-invocations/{inv['id']}/dispatch",
        json={
            "lease": lease["lease"],
            "expected_state_revision": inv["state_revision"],
        },
        headers=worker_auth,
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "GOAL_INFERENCE_CLOSED"
    assert "BLOCKED" in denied.json()["error"]["message"]

    still = client.get(
        "/api/v1/model-invocations",
        params={
            "project_id": goal["project_id"],
            "goal_id": goal["id"],
            "activity_id": activity_id,
        },
        headers=auth,
    )
    assert still.status_code == 200
    row = next(r for r in still.json()["data"] if r["id"] == inv["id"])
    assert row["status"] == "AUTHORIZED"


def test_http_create_orchestration_abandonment(api, objects):
    """第195批：POST 登记放弃 → BLOCKED；幂等 generation；≠ DONE。"""
    client, token, auth, goal, _plan = _start_goal(api, objects)
    goal_id = goal["id"]
    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(abandoner, ["worker"])}

    created = client.post(
        f"/api/v1/goals/{goal_id}/orchestration-abandonments",
        json={
            "reason": "INTENT_EXPIRED",
            "generation": 195,
            "prior_run_id": "wf-195",
        },
        headers=worker_auth,
    )
    assert created.status_code == 201, created.text
    row = created.json()["data"]
    assert row["reason"] == "INTENT_EXPIRED"
    assert row["generation"] == 195
    assert row["prior_run_id"] == "wf-195"
    assert row["marks_goal_done"] is False

    blocked = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"
    assert "ORCHESTRATION_ABANDONED:INTENT_EXPIRED" in (blocked["block_reason"] or "")

    again = client.post(
        f"/api/v1/goals/{goal_id}/orchestration-abandonments",
        json={
            "reason": "INTENT_EXPIRED",
            "generation": 195,
            "prior_run_id": "wf-195",
        },
        headers=worker_auth,
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["id"] == row["id"]

    conflict = client.post(
        f"/api/v1/goals/{goal_id}/orchestration-abandonments",
        json={
            "reason": "RECOVERY_DISABLED",
            "generation": 195,
        },
        headers=worker_auth,
    )
    assert conflict.status_code == 409, conflict.text

    viewer_denied = client.post(
        f"/api/v1/goals/{goal_id}/orchestration-abandonments",
        json={"reason": "INTENT_EXPIRED", "generation": 196},
        headers=auth,
    )
    assert viewer_denied.status_code == 403, viewer_denied.text
    assert token


def test_http_list_and_release_orchestration_abandonment(api, objects):
    """第187批：HTTP 列出放弃事实 + 人工解除 BLOCKED；≠ DONE。"""
    from datetime import UTC, datetime

    client, token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = goal["id"]
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

    abandoner = str(uuid4())
    _register_worker(abandoner, kinds=("PLAN", "EXECUTE"))
    record_orchestration_abandonment(
        engine,
        UUID(goal_id),
        subject=abandoner,
        project_ids=[],
        reason="RECOVERY_DISABLED",
        generation=187,
        prior_run_id="wf-187",
    )

    listed = client.get(
        f"/api/v1/goals/{goal_id}/orchestration-abandonments",
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert len(rows) == 1
    assert rows[0]["reason"] == "RECOVERY_DISABLED"
    assert rows[0]["generation"] == 187
    assert rows[0]["marks_goal_done"] is False
    assert rows[0]["prior_run_id"] == "wf-187"

    blocked = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["status"] != "DONE"

    # 未确认 SHUTDOWN → 409
    early = client.post(
        f"/api/v1/goals/{goal_id}/orchestration-abandonment-release",
        json={
            "expected_state_revision": blocked["state_revision"],
            "reason": "too early before shutdown confirmed",
        },
        headers=auth,
    )
    assert early.status_code == 409, early.text
    assert early.json()["error"]["code"] == "INVALID_STATE"
    assert "STOP_UNCONFIRMED" in early.json()["error"]["message"]

    with engine.begin() as db:
        stop_id = str(
            db.execute(
                text(
                    """SELECT id FROM stops
                    WHERE attempt_id=:id AND reason='SHUTDOWN'"""
                ),
                {"id": attempt_id},
            ).scalar_one()
        )
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

    rev = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"][
        "state_revision"
    ]
    released = client.post(
        f"/api/v1/goals/{goal_id}/orchestration-abandonment-release",
        json={"expected_state_revision": rev, "reason": "operator clear abandon"},
        headers=auth,
    )
    assert released.status_code == 200, released.text
    body = released.json()["data"]
    assert body["status"] == "PLANNING"
    assert body["status"] != "DONE"
    assert body["block_reason"] is None
    assert body["previous_status"] == "BLOCKED"
