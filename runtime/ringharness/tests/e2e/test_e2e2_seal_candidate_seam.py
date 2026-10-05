"""E2E-2 缝：受控 seal_candidate 封存订单仓快照；≠ Goal DONE。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from evidence_ledger.objects import S3Objects
from execution_broker import execute_seal_candidate_prepare, run_seal_candidate_effect
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed

_ALLOW = ["order_service/**", "tests/**", "pyproject.toml", "FIXED_INPUT.json"]


def test_e2e2_seal_candidate_from_order_worktree(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    run = seed.seed_run(run_id="e2e2-seal", runtime_root=tmp_path)
    # 订单仓快照 > 默认 1KiB objects 限额；本缝抬高限额，不改全局 fixture
    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "order-seal",
            "allowed_tools": ["read_file", "seal_candidate"],
            "allowed_paths": _ALLOW,
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
    plan["tasks"][0]["contract"]["allowed_paths"] = list(_ALLOW)
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
            "purpose": "e2e2 seal_candidate",
            "tool_ref": "seal_candidate",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "seal_candidate",
            "tool_schema_digest": SEAL_CANDIDATE_SCHEMA_DIGEST,
            "parameters": {"verification_profile_ids": [profile_id]},
        },
        separators=(",", ":"),
    ).encode()
    oversized_path = run.executor_worktree / "order_service" / "oversized.bin"
    oversized_path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    oversized = execute_seal_candidate_prepare(
        run.executor_worktree,
        input_blob,
        allowed_paths=_ALLOW,
    )
    oversized_path.unlink()
    assert oversized.observed_outcome == "FAILED"
    assert "单文件上限" in (oversized.error or "")

    git_timeout = execute_seal_candidate_prepare(
        run.executor_worktree,
        input_blob,
        allowed_paths=_ALLOW,
        git_timeout_seconds=0,
    )
    assert git_timeout.observed_outcome == "SUCCEEDED"
    assert git_timeout.git_commit is None

    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e2-seal",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "seal_candidate",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]

    host_secret = tmp_path / "seal-host-secret.txt"
    host_secret.write_bytes(b"host-only-seal-secret")
    linked_secret = run.executor_worktree / "order_service" / "linked-secret.txt"
    os.link(host_secret, linked_secret)
    blocked = run_seal_candidate_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=run.executor_worktree,
        allowed_paths=_ALLOW,
        input_bytes=input_blob,
    )
    assert blocked["result"].observed_outcome == "FAILED"
    assert "硬链接" in (blocked["result"].error or "")
    assert blocked["candidate"] is None
    assert host_secret.read_bytes() == b"host-only-seal-secret"
    linked_secret.unlink()

    safe_step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": step.json()["data"]["logical_step_id"],
            "purpose": "e2e2 safe seal_candidate",
            "tool_ref": "seal_candidate",
        },
        headers=worker_auth,
    )
    assert safe_step.status_code == 201, safe_step.text
    safe_prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": safe_step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "seal_candidate",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert safe_prepared.status_code == 201, safe_prepared.text
    effect = safe_prepared.json()["data"]

    ran = run_seal_candidate_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=run.executor_worktree,
        allowed_paths=_ALLOW,
        input_bytes=input_blob,
    )
    assert ran.get("seal_error") is None, ran.get("seal_error")
    assert ran["result"].observed_outcome == "SUCCEEDED", ran["result"].error
    assert ran["candidate"] is not None
    assert ran["candidate"]["git_commit"]
    paths = {f["path"] for f in ran["candidate"]["files"]}
    assert "order_service/store.py" in paths
    assert "tests/test_idempotency.py" in paths

    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"

    summary = {
        "phase": "E2E-2-seal-candidate",
        "run_id": run.run_id,
        "effect_id": effect["id"],
        "goal_id": goal["id"],
        "candidate_manifest_id": ran["candidate"]["id"],
        "file_count": len(ran["candidate"]["files"]),
        "marks_goal_done": False,
        "non_goals": ["official_loop_multistep", "audit_pass", "goal_done"],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
