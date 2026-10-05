"""E2E-2 compose：run_tests(红)→write_file→run_tests(绿)→git_diff→seal→outcome；≠ Goal DONE。

本缝由测试宿主按固定顺序调用受控工具，**不宣称**官方 AgentLoop 自主决策多步。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import (
    GIT_DIFF_SCHEMA_DIGEST,
    RUN_TESTS_SCHEMA_DIGEST,
    SEAL_CANDIDATE_SCHEMA_DIGEST,
    WRITE_FILE_SCHEMA_DIGEST,
)
from control_kernel.storage.artifacts import Artifacts
from evidence_ledger.objects import S3Objects
from execution_broker import (
    run_git_diff_effect,
    run_run_tests_effect,
    run_seal_candidate_effect,
    run_write_file_effect,
)
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

_FIX_STORE = (
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


def _setup_execute(api, objects, tmp_path: Path):
    run = seed.seed_run(run_id="e2e2-compose", runtime_root=tmp_path)
    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "order-compose",
            "allowed_tools": [
                "read_file",
                "write_file",
                "run_tests",
                "git_diff",
                "seal_candidate",
            ],
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
    return {
        "run": run,
        "store": store,
        "client": client,
        "auth": auth,
        "goal": goal,
        "profile_id": profile_id,
        "worker_auth": worker_auth,
        "exec_lease": exec_lease,
        "project_id": UUID(exec_lease["activity"]["project_id"]),
        "engine": client.app.state.engine,
        "workspace": run.executor_worktree,
    }


def _tool_step(
    ctx: dict,
    *,
    tool_ref: str,
    purpose: str,
    parameters: dict,
    schema_digest: str,
    predecessor_step_id: str | None,
):
    client = ctx["client"]
    worker_auth = ctx["worker_auth"]
    exec_lease = ctx["exec_lease"]
    activity_id = exec_lease["activity"]["id"]
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": predecessor_step_id,
            "purpose": purpose,
            "tool_ref": tool_ref,
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    input_blob = json.dumps(
        {
            "tool_ref": tool_ref,
            "tool_schema_digest": schema_digest,
            "parameters": parameters,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert len(input_blob) < 1024, (tool_ref, len(input_blob))
    input_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
        ctx["project_id"],
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity=f"test:compose-{tool_ref}",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": tool_ref,
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    return step_data, prepared.json()["data"], input_blob


def test_e2e2_compose_red_write_green_diff_seal(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = _setup_execute(api, objects, tmp_path)
    client, auth, goal = ctx["client"], ctx["auth"], ctx["goal"]
    lease = ctx["exec_lease"]["lease"]
    ws = ctx["workspace"]
    pred = None
    trail: list[str] = []

    # 1) run_tests → 红
    step, effect, blob = _tool_step(
        ctx,
        tool_ref="run_tests",
        purpose="compose public red",
        parameters={"suite": "public"},
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        predecessor_step_id=pred,
    )
    pred = step["id"]
    red = run_run_tests_effect(
        client,
        worker_auth=ctx["worker_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        input_bytes=blob,
    )
    assert red["result"].exit_code != 0
    trail.append("run_tests_red")

    # 2) write_file → 幂等修复
    step, effect, blob = _tool_step(
        ctx,
        tool_ref="write_file",
        purpose="compose fix store",
        parameters={"path": "order_service/store.py", "content": _FIX_STORE},
        schema_digest=WRITE_FILE_SCHEMA_DIGEST,
        predecessor_step_id=pred,
    )
    pred = step["id"]
    wrote = run_write_file_effect(
        client,
        worker_auth=ctx["worker_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        allowed_paths=["order_service/**"],
        input_bytes=blob,
    )
    assert wrote["result"].observed_outcome == "SUCCEEDED", wrote["result"].error
    trail.append("write_file")

    # 3) run_tests → 绿
    step, effect, blob = _tool_step(
        ctx,
        tool_ref="run_tests",
        purpose="compose public green",
        parameters={"suite": "public"},
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        predecessor_step_id=pred,
    )
    pred = step["id"]
    green = run_run_tests_effect(
        client,
        worker_auth=ctx["worker_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        input_bytes=blob,
    )
    assert green["result"].exit_code == 0, green["result"].stdout_text
    trail.append("run_tests_green")

    # 4) git_diff → dirty
    step, effect, blob = _tool_step(
        ctx,
        tool_ref="git_diff",
        purpose="compose diff",
        parameters={},
        schema_digest=GIT_DIFF_SCHEMA_DIGEST,
        predecessor_step_id=pred,
    )
    pred = step["id"]
    diffed = run_git_diff_effect(
        client,
        worker_auth=ctx["worker_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        input_bytes=blob,
    )
    assert diffed["result"].dirty is True
    trail.append("git_diff")

    # 5) seal_candidate
    step, effect, blob = _tool_step(
        ctx,
        tool_ref="seal_candidate",
        purpose="compose seal",
        parameters={"verification_profile_ids": [ctx["profile_id"]]},
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        predecessor_step_id=pred,
    )
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=ctx["worker_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        allowed_paths=_ALLOW,
        input_bytes=blob,
    )
    assert sealed.get("seal_error") is None, sealed.get("seal_error")
    assert sealed["candidate"] is not None
    candidate_id = sealed["candidate"]["id"]
    trail.append("seal_candidate")

    goal_mid = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_mid.json()["data"]["status"] != "DONE"

    # 6) EXECUTE outcome → Task VERIFYING；工程写入关闭；Goal 仍 ≠ DONE
    activity_now = client.get(
        f"/api/v1/activities/{ctx['exec_lease']['activity']['id']}",
        headers=auth,
    )
    assert activity_now.status_code == 200, activity_now.text
    expected_rev = activity_now.json()["data"]["state_revision"]
    outcome = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/outcomes",
        json={
            "lease": lease,
            "expected_state_revision": expected_rev,
            "outcome": {
                "candidate_manifest_id": candidate_id,
                "evidence_ids": sealed["result_artifact_ids"],
            },
        },
        headers=ctx["worker_auth"],
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["data"]["status"] == "SUCCEEDED"
    trail.append("execute_outcome")

    task_id = ctx["exec_lease"]["activity"]["task_id"]
    task = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task["status"] == "VERIFYING"

    # 封存并进入 VERIFYING 后，新 write_file prepare 应失败关闭
    step_closed = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/steps",
        json={
            "lease": lease,
            "predecessor_step_id": step["id"],
            "purpose": "should fail closed",
            "tool_ref": "write_file",
        },
        headers=ctx["worker_auth"],
    )
    # Activity 已 SUCCEEDED 时可能拒建 step；或 prepare 拒
    if step_closed.status_code == 201:
        bad_blob = json.dumps(
            {
                "tool_ref": "write_file",
                "tool_schema_digest": WRITE_FILE_SCHEMA_DIGEST,
                "parameters": {"path": "order_service/store.py", "content": "x\n"},
            },
            separators=(",", ":"),
        ).encode()
        bad_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
            ctx["project_id"],
            "sha256:" + hashlib.sha256(bad_blob).hexdigest(),
            BytesIO(bad_blob),
            mime="application/json",
            producer_identity="test:compose-closed",
        )
        bad_prep = client.post(
            "/internal/v1/effects/prepare",
            json={
                "lease": lease,
                "logical_step_id": step_closed.json()["data"]["logical_step_id"],
                "intent_revision": 1,
                "tool_ref": "write_file",
                "input_artifact_id": str(bad_art.id),
            },
            headers=ctx["worker_auth"],
        )
        assert bad_prep.status_code >= 400, bad_prep.text
        trail.append("write_closed")
    else:
        assert step_closed.status_code >= 400, step_closed.text
        trail.append("step_closed")

    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"

    summary = {
        "phase": "E2E-2-compose",
        "run_id": ctx["run"].run_id,
        "goal_id": goal["id"],
        "candidate_manifest_id": candidate_id,
        "trail": trail,
        "marks_goal_done": False,
        "non_goals": [
            "official_agent_loop_decision",
            "auditor_pass",
            "goal_done",
        ],
    }
    (ctx["run"].artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
