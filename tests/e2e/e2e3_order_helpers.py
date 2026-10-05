"""E2E-3 订单仓共用：seed + EXECUTE claim 到可 seal 状态。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

from evidence_ledger.objects import S3Objects
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed

ALLOW = ["order_service/**", "tests/**", "pyproject.toml", "FIXED_INPUT.json"]


def run_pytest(cwd: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def boot_execute_for_seal(
    api,
    objects,
    tmp_path: Path,
    *,
    run_id: str,
    allowed_tools: list[str] | None = None,
) -> dict:
    """种子订单仓 + PLAN/EXECUTE claim，返回可 prepare seal_candidate 的上下文。"""
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        raise RuntimeError("RING_TEST_DATABASE_URL required")

    tools = allowed_tools or ["read_file", "write_file", "seal_candidate"]
    run = seed.seed_run(run_id=run_id, runtime_root=tmp_path)
    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": f"e2e3-{run_id}",
            "allowed_tools": list(tools),
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
        "profile_id": profile_id,
        "exec_subject": exec_subject,
        "exec_auth": exec_auth,
        "exec_lease": exec_lease,
        "project_id": UUID(exec_lease["activity"]["project_id"]),
        "engine": client.app.state.engine,
        "seed": seed,
    }
