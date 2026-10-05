"""E2E-2 缝：受控 run_tests 对订单仓公开套件跑红；≠ Goal DONE。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import RUN_TESTS_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_run_tests_effect
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed


def test_e2e2_run_tests_public_suite_red_on_buggy_order(
    api, objects, tmp_path: Path, monkeypatch
):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    run = seed.seed_run(run_id="e2e2-run-tests", runtime_root=tmp_path)
    host_home = tmp_path / "broker-host-home"
    host_home.mkdir()
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("RING_E2E_SECRET_SENTINEL", "must-not-enter-candidate")
    (run.executor_worktree / "tests" / "test_00_broker_env.py").write_text(
        f"""import os


def test_broker_environment_isolated():
    failures = []
    if os.environ.get("RING_E2E_SECRET_SENTINEL"):
        failures.append("BROKER" + "_SECRET_VISIBLE")
    if os.environ.get("HOME") == {str(host_home)!r}:
        failures.append("BROKER" + "_HOME_VISIBLE")
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1":
        failures.append("PYTEST" + "_PLUGIN_AUTOLOAD_ENABLED")
    assert not failures, ",".join(failures)
""",
        encoding="utf-8",
    )
    pytest_hijack_sentinel = tmp_path / "candidate-pytest-module-ran"
    (run.executor_worktree / "pytest.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(pytest_hijack_sentinel)!r}).write_text('hijacked')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (run.executor_worktree / "tests" / "test_output_budget.py").write_text(
        """def test_output_budget():
    print("RING-BIG-OUTPUT-BEGIN")
    print("x" * 400_000)
    print("RING-BIG-OUTPUT-END")
    assert False, "force captured output into pytest report"
""",
        encoding="utf-8",
    )

    store, _, _ = objects
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "order-run-tests",
            "allowed_tools": ["read_file", "run_tests"],
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
            "purpose": "e2e2 run_tests public",
            "tool_ref": "run_tests",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "run_tests",
            "tool_schema_digest": RUN_TESTS_SCHEMA_DIGEST,
            "parameters": {"suite": "public"},
        },
        separators=(",", ":"),
    ).encode()
    assert len(input_blob) < 1024, len(input_blob)
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e2-run-tests",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "run_tests",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]

    ran = run_run_tests_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=run.executor_worktree,
        input_bytes=input_blob,
        timeout_seconds=120,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED", ran["result"].error
    assert ran["result"].exit_code != 0
    assert pytest_hijack_sentinel.exists() is False
    assert ran["result"].content is not None
    assert len(ran["result"].content) <= 1024
    meta = json.loads(ran["result"].content.decode())
    assert meta["suite"] == "public"
    assert meta["exit_code"] != 0
    retained_stdout = ran["result"].stdout_text.encode("utf-8")
    assert len(retained_stdout) == 256_000
    retained_digest = "sha256:" + hashlib.sha256(retained_stdout).hexdigest()
    assert meta["stdout_digest"] != retained_digest
    assert meta["stdout_bytes"] > len(retained_stdout)
    assert meta["stdout_truncated"] is True
    process_output = ran["result"].stdout_text + ran["result"].stderr_text
    assert "BROKER_SECRET_VISIBLE" not in process_output
    assert "BROKER_HOME_VISIBLE" not in process_output
    assert "PYTEST_PLUGIN_AUTOLOAD_ENABLED" not in process_output

    got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert got.json()["data"]["status"] == "SUCCEEDED"

    timeout_step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": step.json()["data"]["logical_step_id"],
            "purpose": "e2e2 run_tests typed timeout",
            "tool_ref": "run_tests",
        },
        headers=worker_auth,
    )
    assert timeout_step.status_code == 201, timeout_step.text
    timeout_prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": timeout_step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "run_tests",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert timeout_prepared.status_code == 201, timeout_prepared.text
    timeout_effect = timeout_prepared.json()["data"]
    timeout_run = run_run_tests_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=timeout_effect,
        project_id=project_id,
        workspace_root=run.executor_worktree,
        input_bytes=input_blob,
        timeout_seconds=0,
    )
    assert timeout_run["result"].observed_outcome == "FAILED"
    assert timeout_run["result"].exit_code == 124
    assert timeout_run["result"].timed_out is True
    timeout_got = client.get(
        f"/api/v1/effects/{timeout_effect['id']}", headers=auth
    )
    assert timeout_got.json()["data"]["status"] == "FAILED"

    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"

    summary = {
        "phase": "E2E-2-run-tests",
        "run_id": run.run_id,
        "effect_id": effect["id"],
        "goal_id": goal["id"],
        "public_suite_exit_code": meta["exit_code"],
        "marks_goal_done": False,
        "non_goals": ["git_diff", "seal_candidate", "official_loop_multistep", "goal_done"],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
