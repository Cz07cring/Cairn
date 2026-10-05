"""E2E-5 首缝：订单 write_file PREPARED 后失租 → 复用原 effect_id，不重复写入。

对齐研究案例 E2E-5 注入点 1；10 次失租/重领后仍单 effect，再唯一 dispatch+write。
≠ Goal DONE；不宣称其余五注入点 / 自主 Loop。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import WRITE_FILE_SCHEMA_DIGEST
from e2e3_order_helpers import ALLOW
from e2e_tool_helpers import load_reference_fix_store, prepare_tool_effect
from evidence_ledger.objects import S3Objects
from execution_broker import run_write_file_effect
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed

_CRASH_ROUNDS = 10


def _force_expire(engine, attempt_id: str) -> None:
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )


def _trigger_expire_scan(client, token) -> None:
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


def _confirm_stop(client, worker_auth, stop_id: str, attempt_id: str) -> None:
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


def test_e2e5_order_write_prepare_crash_reuses_effect_x10(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    run = seed.seed_run(run_id="e2e5-prep-crash", runtime_root=tmp_path)
    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)
    ws = run.executor_worktree
    buggy = (ws / "order_service" / "store.py").read_text(encoding="utf-8")
    assert "_by_key" not in buggy
    fix_body = load_reference_fix_store()

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "e2e5-order-prep-crash",
            "allowed_tools": ["read_file", "write_file", "run_tests", "seal_candidate"],
            "allowed_paths": list(ALLOW),
            "protected_paths": [],
            "network_allowlist": [],
            "external_actions": [],
            "secret_scope_refs": [],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert policy.status_code == 201, policy.text
    goal_body = {**goal_body, "policy_id": policy.json()["data"]["id"]}
    _drain_ready(client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE", "PLAN"))
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
    plan_activity = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    ).json()["data"][0]

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
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
    eng.dispose()

    subject_a = str(uuid4())
    _register_worker(subject_a, kinds=("PLAN", "EXECUTE"))
    auth_a = {"Authorization": "Bearer " + token(subject_a, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**auth_a, "Idempotency-Key": str(uuid4())},
    )
    lease = claimed.json()["data"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    plan = _plan_body(goal, profile_id)
    plan["tasks"][0]["contract"]["allowed_paths"] = list(ALLOW)
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=auth_a,
    )
    assert done.status_code == 200, done.text
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    eng.dispose()
    exec_lease = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**auth_a, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    ctx = {
        "client": client,
        "engine": engine,
        "store": store,
        "project_id": project_id,
        "exec_lease": exec_lease,
        "exec_auth": auth_a,
    }
    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="write_file",
        purpose="e2e5 write prepare crash",
        parameters={"path": "order_service/store.py", "content": fix_body},
        schema_digest=WRITE_FILE_SCHEMA_DIGEST,
        predecessor_step_id=None,
        producer="e2e5",
    )
    effect_id = effect["id"]
    logical_step_id = step["logical_step_id"]
    input_artifact_id = effect["input_artifact_id"]
    assert effect["status"] == "PREPARED"
    assert (ws / "order_service" / "store.py").read_text(encoding="utf-8") == buggy

    holder_auth = auth_a
    holder_lease = exec_lease
    reused: list[str] = []

    for _round_i in range(_CRASH_ROUNDS):
        attempt_id = holder_lease["lease"]["attempt_id"]
        _force_expire(engine, attempt_id)
        _trigger_expire_scan(client, token)

        with engine.begin() as db:
            stop = (
                db.execute(
                    text(
                        """SELECT id, status FROM stops
                        WHERE attempt_id=:id AND reason='LEASE_EXPIRED'
                        ORDER BY created_at DESC LIMIT 1"""
                    ),
                    {"id": attempt_id},
                )
                .mappings()
                .one()
            )
            effect_n = db.execute(
                text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
                {"a": activity_id},
            ).scalar_one()
            status = db.execute(
                text("SELECT status FROM effect_intents WHERE id=:id"),
                {"id": effect_id},
            ).scalar_one()
        assert status == "PREPARED"
        assert effect_n == 1
        assert stop["status"] == "REQUESTED"

        subject_b = str(uuid4())
        _register_worker(subject_b, kinds=("EXECUTE",))
        auth_b = {"Authorization": "Bearer " + token(subject_b, ["worker"])}
        reclaimed = client.post(
            "/internal/v1/claims",
            json={"kinds": ["EXECUTE"], "capabilities": []},
            headers={**auth_b, "Idempotency-Key": str(uuid4())},
        )
        assert reclaimed.status_code == 200, reclaimed.text
        new_lease = reclaimed.json()["data"]
        assert new_lease["activity"]["id"] == activity_id

        again = client.post(
            "/internal/v1/effects/prepare",
            json={
                "lease": new_lease["lease"],
                "logical_step_id": logical_step_id,
                "intent_revision": 1,
                "tool_ref": "write_file",
                "input_artifact_id": input_artifact_id,
            },
            headers=auth_b,
        )
        assert again.status_code == 201, again.text
        assert again.json()["data"]["id"] == effect_id
        reused.append(again.json()["data"]["id"])

        blocked = client.post(
            f"/internal/v1/effects/{effect_id}/dispatch",
            json={
                "lease": new_lease["lease"],
                "effect_state_revision": again.json()["data"]["state_revision"],
            },
            headers=auth_b,
        )
        assert blocked.status_code == 422, blocked.text

        _confirm_stop(client, holder_auth, str(stop["id"]), attempt_id)
        holder_auth = auth_b
        holder_lease = new_lease

    assert reused == [effect_id] * _CRASH_ROUNDS
    assert (ws / "order_service" / "store.py").read_text(encoding="utf-8") == buggy

    # Stop 确认后：幂等 prepare 绑定当前 attempt，再唯一执行写入
    prep = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": holder_lease["lease"],
            "logical_step_id": logical_step_id,
            "intent_revision": 1,
            "tool_ref": "write_file",
            "input_artifact_id": input_artifact_id,
        },
        headers=holder_auth,
    )
    assert prep.status_code == 201, prep.text
    effect = prep.json()["data"]
    assert effect["id"] == effect_id

    wrote = run_write_file_effect(
        client,
        worker_auth=holder_auth,
        lease=holder_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=ws,
        allowed_paths=["order_service/**"],
        input_bytes=blob,
    )
    assert wrote["result"].observed_outcome == "SUCCEEDED", wrote["result"].error
    on_disk = (ws / "order_service" / "store.py").read_text(encoding="utf-8")
    assert on_disk == fix_body

    with engine.begin() as db:
        effect_n = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
        statuses = list(
            db.execute(
                text(
                    """SELECT status FROM effect_intents
                    WHERE activity_id=:a ORDER BY created_at"""
                ),
                {"a": activity_id},
            ).scalars()
        )
    assert effect_n == 1
    assert statuses == ["SUCCEEDED"]
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"

    summary = {
        "phase": "E2E-5-prepare-crash-order-write",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "effect_id": effect_id,
        "crash_rounds": _CRASH_ROUNDS,
        "effect_count": 1,
        "write_count": 1,
        "marks_goal_done": False,
        "non_goals": [
            "e2e5_other_inject_points",
            "official_loop_autonomous",
            "goal_done",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    engine.dispose()
