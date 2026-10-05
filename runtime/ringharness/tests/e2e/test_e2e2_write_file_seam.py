"""E2E-2 首缝：受控 write_file 写入订单仓，公开幂等测由红转绿；≠ Goal DONE。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import WRITE_FILE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
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


def test_e2e2_write_file_turns_public_idempotency_green(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    run = seed.seed_run(run_id="e2e2-write-file", runtime_root=tmp_path)
    public_red = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_idempotency.py"],
        cwd=run.executor_worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert public_red.returncode != 0, public_red.stdout + public_red.stderr

    store, _, _ = objects
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "order-write",
            "allowed_tools": ["read_file", "write_file"],
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

    # objects fixture 默认 max_bytes=1024；整份 input JSON 须落在限额内。
    fix_text = (
        "from uuid import uuid4\n"
        "from order_service.models import Order\n"
        "INITIAL_INVENTORY={'SKU-001':10}\n"
        "class OrderStore:\n"
        " def __init__(self):\n"
        "  self.inventory=dict(INITIAL_INVENTORY); self.orders={}; self._by_key={}\n"
        " def reset(self):\n"
        "  self.inventory=dict(INITIAL_INVENTORY); self.orders={}; self._by_key={}\n"
        " def create_order(self,*,sku,quantity,unit_price,idempotency_key):\n"
        "  e=self._by_key.get(idempotency_key)\n"
        "  if e is not None: return e\n"
        "  if self.inventory[sku]<quantity: raise ValueError('insufficient inventory')\n"
        "  self.inventory[sku]-=quantity\n"
        "  o=Order(order_id=str(uuid4()),sku=sku,quantity=quantity,"
        "unit_price=unit_price,idempotency_key=idempotency_key)\n"
        "  self.orders[o.order_id]=o; self._by_key[idempotency_key]=o; return o\n"
    )
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "e2e2 write_file 幂等修复",
            "tool_ref": "write_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "write_file",
            "tool_schema_digest": WRITE_FILE_SCHEMA_DIGEST,
            "parameters": {
                "path": "order_service/store.py",
                "content": fix_text,
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert len(input_blob) < 1024, len(input_blob)
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e2-write",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "write_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]

    ran = run_write_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=run.executor_worktree,
        allowed_paths=["order_service/**"],
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED", ran["result"].error
    assert (run.executor_worktree / "order_service" / "store.py").read_text(
        encoding="utf-8"
    ) == fix_text

    public_green = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/"],
        cwd=run.executor_worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert public_green.returncode == 0, public_green.stdout + public_green.stderr

    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"

    summary = {
        "phase": "E2E-2-write-file",
        "run_id": run.run_id,
        "effect_id": effect["id"],
        "goal_id": goal["id"],
        "marks_goal_done": False,
        "non_goals": ["run_tests_tool", "seal_candidate", "official_loop_write", "goal_done"],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
