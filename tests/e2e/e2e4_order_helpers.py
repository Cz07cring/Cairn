"""E2E-4 订单仓：带 GLOBAL Goal 标准的 boot，供 FINALIZE。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from e2e3_order_helpers import ALLOW, run_pytest
from evidence_ledger.objects import S3Objects
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_finalization import _ready_project_with_global
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed


def boot_execute_for_finalize(api, objects, tmp_path: Path, *, run_id: str) -> dict:
    """订单仓 + Task MECHANICAL / Goal GLOBAL；EXECUTE claim 至可 seal。"""
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        raise RuntimeError("RING_TEST_DATABASE_URL required")

    run = seed.seed_run(run_id=run_id, runtime_root=tmp_path)
    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)

    client, token, auth, _project, goal_body, mechanical_id, global_profile_id = (
        _ready_project_with_global(api, objects)
    )
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": f"e2e4-{run_id}",
            "allowed_tools": [
                "read_file",
                "write_file",
                "run_tests",
                "git_diff",
                "seal_candidate",
            ],
            "allowed_paths": ALLOW,
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
    # Goal 合同须为 GLOBAL；Task 验收用 MECHANICAL
    assert goal["contract"]["success_criteria"][0]["id"] == "C1"
    assert goal["contract"]["success_criteria"][0]["verification_profile_id"] == global_profile_id

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

    exec_subject = str(uuid4())
    _register_worker(exec_subject, kinds=("PLAN", "EXECUTE"))
    exec_auth = {"Authorization": "Bearer " + token(exec_subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**exec_auth, "Idempotency-Key": str(uuid4())},
    )
    lease = claimed.json()["data"]
    plan = _plan_body(goal, mechanical_id)
    plan["coverage"][0]["verification_profile_id"] = mechanical_id
    plan["tasks"][0]["contract"]["allowed_paths"] = list(ALLOW)
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=exec_auth,
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
        headers={**exec_auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    client.app.state.objects = store
    return {
        "run": run,
        "store": store,
        "client": client,
        "token": token,
        "auth": auth,
        "goal": goal,
        "profile_id": mechanical_id,
        "mechanical_id": mechanical_id,
        "global_profile_id": global_profile_id,
        "exec_subject": exec_subject,
        "exec_auth": exec_auth,
        "exec_lease": exec_lease,
        "project_id": UUID(exec_lease["activity"]["project_id"]),
        "engine": client.app.state.engine,
        "seed": seed,
    }


__all__ = ["ALLOW", "boot_execute_for_finalize", "run_pytest", "seed"]
