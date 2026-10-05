"""Broker run_tests：批准 suite argv → 证据工件 → TrustedReceipt；≠ Goal DONE。"""

from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.domain.tool_capability_manifest import RUN_TESTS_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_run_tests_effect
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body


def test_broker_run_tests_red_suite_and_not_done(api, objects, tmp_path: Path):
    store, _, _ = objects
    workspace = tmp_path / "repo"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_fail.py").write_text(
        "def test_fail():\n    assert False\n",
        encoding="utf-8",
    )

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "run-tests-e2e",
            "allowed_tools": ["read_file", "run_tests"],
            "allowed_paths": ["tests/**"],
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
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    plan = _plan_body(goal, profile_id)
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
    exec_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert exec_claim.status_code == 200, exec_claim.text
    exec_lease = exec_claim.json()["data"]
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "run_tests public",
            "tool_ref": "run_tests",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    logical_step_id = step.json()["data"]["logical_step_id"]

    # EXECUTE 禁止 suite=auditor（三权：隐藏测仅验证活动）
    auditor_blob = json.dumps(
        {
            "tool_ref": "run_tests",
            "tool_schema_digest": RUN_TESTS_SCHEMA_DIGEST,
            "parameters": {"suite": "auditor"},
        },
        separators=(",", ":"),
    ).encode()
    auditor_digest = "sha256:" + hashlib.sha256(auditor_blob).hexdigest()
    auditor_art = Artifacts(engine, store).ingest_raw(
        project_id,
        auditor_digest,
        BytesIO(auditor_blob),
        mime="application/json",
        producer_identity="test:run-tests-auditor-deny",
    )
    denied = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": logical_step_id,
            "intent_revision": 1,
            "tool_ref": "run_tests",
            "input_artifact_id": str(auditor_art.id),
        },
        headers=worker_auth,
    )
    assert denied.status_code == 422, denied.text
    assert "auditor" in denied.text.lower() or "EXECUTE" in denied.text

    input_blob = json.dumps(
        {
            "tool_ref": "run_tests",
            "tool_schema_digest": RUN_TESTS_SCHEMA_DIGEST,
            "parameters": {"suite": "public"},
        },
        separators=(",", ":"),
    ).encode()
    assert len(input_blob) < 1024, len(input_blob)
    input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        input_digest,
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:run-tests-input",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": logical_step_id,
            "intent_revision": 1,
            "tool_ref": "run_tests",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert effect["tool_ref"] == "run_tests"
    assert effect["replay_class"] == "READ_ONLY"

    ran = run_run_tests_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=workspace,
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED"
    assert ran["result"].exit_code != 0
    assert ran["result_artifact_ids"]
    meta = json.loads(ran["result"].content.decode())
    assert meta["suite"] == "public"
    assert meta["exit_code"] != 0
    assert meta["argv"][:5] == ["python", "-P", "-m", "pytest", "-q"]

    got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert got.json()["data"]["status"] == "SUCCEEDED"
    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"
