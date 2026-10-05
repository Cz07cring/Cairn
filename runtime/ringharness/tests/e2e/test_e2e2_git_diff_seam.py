"""E2E-2 缝：受控 git_diff 观察订单仓 write 后的脏工作树；≠ Goal DONE。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import GIT_DIFF_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_git_diff_effect
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed


def test_e2e2_git_diff_sees_dirty_after_store_edit(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    run = seed.seed_run(run_id="e2e2-git-diff", runtime_root=tmp_path)
    # 模拟 Executor 改动（本缝只验 git_diff，不宣称 write_file 串联）
    store_py = run.executor_worktree / "order_service" / "store.py"
    store_py.write_text(store_py.read_text(encoding="utf-8") + "\n# e2e2-git-diff\n", encoding="utf-8")

    store, _, _ = objects
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "order-git-diff",
            "allowed_tools": ["read_file", "git_diff"],
            "allowed_paths": ["order_service/**", "tests/**", "pyproject.toml", "FIXED_INPUT.json"],
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

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    lease = claimed.json()["data"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    plan = _plan_body(goal, profile_id)
    plan["tasks"][0]["contract"]["allowed_paths"] = [
        "order_service/**",
        "tests/**",
        "pyproject.toml",
        "FIXED_INPUT.json",
    ]
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=worker_auth,
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
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "e2e2 git_diff",
            "tool_ref": "git_diff",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "git_diff",
            "tool_schema_digest": GIT_DIFF_SCHEMA_DIGEST,
            "parameters": {},
        },
        separators=(",", ":"),
    ).encode()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e2-git-diff",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "git_diff",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]

    ran = run_git_diff_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=run.executor_worktree,
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED", ran["result"].error
    assert ran["result"].dirty is True
    assert ran["result"].base_commit
    assert ran["result"].content is not None
    assert len(ran["result"].content) <= 1024
    meta = json.loads(ran["result"].content.decode())
    assert meta["dirty"] is True
    assert "store.py" in meta["status_preview"]

    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"

    summary = {
        "phase": "E2E-2-git-diff",
        "run_id": run.run_id,
        "effect_id": effect["id"],
        "goal_id": goal["id"],
        "base_commit": meta["base_commit"],
        "dirty": True,
        "marks_goal_done": False,
        "non_goals": ["seal_candidate", "official_loop_multistep", "goal_done"],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
