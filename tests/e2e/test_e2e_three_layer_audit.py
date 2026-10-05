"""E2E：订单候选须 MECHANICAL+SEMANTIC+ADVERSARIAL 三层 AUDIT 全 PASS 才 Task DONE。

缺任一层时 Task 保持非 DONE；Goal 绝非 DONE。≠ 官方 Loop 自主 / FINALIZE。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from e2e3_order_helpers import ALLOW, run_pytest
from evidence_ledger.content import encode
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_seal_candidate_effect,
)
from sqlalchemy import create_engine, text
from test_claims import _register_worker
from test_plans import _plan_body
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)

_ROOT = Path(__file__).resolve().parents[2]
_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"
_LAYERS = ("MECHANICAL", "SEMANTIC", "ADVERSARIAL")
_LAYER_ACCEPTANCE = {
    "MECHANICAL": "A-MECH",
    "SEMANTIC": "A-SEM",
    "ADVERSARIAL": "A-ADV",
}


def _seed_layer_profile(client, auth, project_id: str, *, layer: str) -> str:
    """按夹具层定义入库 VerifierDefinition + VerificationProfile。"""
    examples = json.loads(
        (_ROOT / "doc/contracts/verifier-fixtures-v2.json").read_text(encoding="utf-8")
    )["examples"]
    example = next(ex for ex in examples if ex["layer"] == layer)
    definition = {**example["definition"], "project_id": project_id}
    digest = (
        "sha256:"
        + hashlib.sha256(
            encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": "VerifierDefinition",
                        "content": definition,
                        "reference_bindings": [],
                    }
                )
            )
        ).hexdigest()
    )
    ref = f"approved.order.{layer.lower()}.{uuid4().hex[:8]}"
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""INSERT INTO verifier_definitions(
              project_id,ref,content_digest,content,approval_record_digest)
            VALUES(:project,:ref,:digest,CAST(:content AS jsonb),:approval)"""),
            {
                "project": project_id,
                "ref": ref,
                "digest": digest,
                "content": json.dumps(definition),
                "approval": "sha256:" + "e" * 64,
            },
        )
    engine.dispose()
    profile_body = {
        **example["profile"],
        "project_id": project_id,
        "name": f"order-{layer.lower()}-{uuid4().hex[:8]}",
        "verifier_ref": ref,
        "verifier_digest": digest,
        "required_layers": [layer],
    }
    for drop in ("criteria", "cases"):
        profile_body.pop(drop, None)
    created = client.post(
        "/api/v1/verification-profiles",
        json=profile_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


def _three_layer_plan(goal, profiles: dict[str, str]) -> dict:
    """三 acceptance + 覆盖挂 MECHANICAL（满足 goal criterion 覆盖一致性）。"""
    plan = _plan_body(goal, profiles["MECHANICAL"])
    plan["tasks"][0]["contract"]["allowed_paths"] = list(ALLOW)
    plan["tasks"][0]["contract"]["acceptance"] = [
        {
            "id": "A-MECH",
            "description": "公开测试与机械检查",
            "required": True,
            "verification_profile_id": profiles["MECHANICAL"],
        },
        {
            "id": "A-SEM",
            "description": "顺序/并发幂等语义",
            "required": True,
            "verification_profile_id": profiles["SEMANTIC"],
        },
        {
            "id": "A-ADV",
            "description": "反作弊与隐藏测隔离",
            "required": True,
            "verification_profile_id": profiles["ADVERSARIAL"],
        },
    ]
    plan["coverage"][0]["task_acceptance_id"] = "A-MECH"
    plan["coverage"][0]["verification_profile_id"] = profiles["MECHANICAL"]
    return plan


def _pass_next_audit(
    *,
    client,
    token,
    auth,
    goal_id: str,
    store,
    engine,
    project_id: UUID,
    candidate: dict,
) -> str:
    """领取下一 READY AUDIT 并 PASS；返回其 layer。"""
    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    audit_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    with create_engine(os.environ["RING_TEST_DATABASE_URL"]).begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='AUDIT' AND goal_id<>:goal"""
            ),
            {"goal": goal_id},
        )
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**audit_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["goal_id"] == goal_id
    assignment = lease["activity"]["verification_assignments"][0]
    layer = assignment["layer"]
    acceptance_id = _LAYER_ACCEPTANCE[layer]
    binding = lease["activity"]["binding"]
    receipt_id = receipt_artifact(engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    ev = str(uuid4())
    if layer == "MECHANICAL":
        observations = None
    else:
        # SEMANTIC / ADVERSARIAL：夹具要求 score_bp + critical_violation
        observations = [
            {
                "criterion_id": acceptance_id,
                "metric": "score_bp",
                "value": "9000",
                "status": "OBSERVED",
                "evidence_ids": [ev],
                "reason_code": "MEASURED",
            },
            {
                "criterion_id": acceptance_id,
                "metric": "critical_violation",
                "value": "false",
                "status": "OBSERVED",
                "evidence_ids": [ev],
                "reason_code": "MEASURED",
            },
        ]
    run_id = post_verification_run(
        client,
        audit_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        criterion_id=acceptance_id,
        engine=engine,
        observations=observations,
    )
    outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "CANDIDATE",
                "audit": {
                    "subject_candidate_manifest_id": candidate["id"],
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "task_contract_revision": binding["task_contract_revision"],
                    "verification_profile_id": assignment["verification_profile_id"],
                    "layer": assignment["layer"],
                    "audit_round": assignment["audit_round"],
                    "verifier_run_ids": [run_id],
                    "verdict": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": acceptance_id,
                            "verdict": "PASS",
                            "evidence_ids": [],
                            "reason": f"{layer} layer pass",
                        }
                    ],
                    "evidence_ids": [],
                    "reason": f"e2e three-layer {layer}",
                },
            },
        },
        headers=audit_auth,
    )
    assert outcome.status_code == 200, outcome.text
    return layer


def test_e2e_order_three_layer_audit_all_required_for_task_done(
    api, objects, tmp_path: Path
):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    # 先 boot 拿 project，再种三层 profile，再自定义 PLAN（覆盖默认单层）
    # boot_execute_for_seal 已 PLAN+EXECUTE；改为手工：部分复用 boot 前半逻辑太重。
    # 策略：boot 后无法改已发布 plan——须在 PLAN outcome 前注入。
    # 扩展：临时用 boot 的 _ready 路径——复制 boot 并替换 plan。

    from e2e3_order_helpers import seed as seed_mod
    from evidence_ledger.objects import S3Objects
    from test_claims import _drain_ready
    from test_goals import _ready_project

    run = seed_mod.seed_run(run_id="e2e-three-layer", runtime_root=tmp_path)
    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    project_id_str = goal_body["project_id"]
    profiles = {
        layer: _seed_layer_profile(client, auth, project_id_str, layer=layer)
        for layer in _LAYERS
    }
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": project_id_str,
            "name": "e2e-three-layer",
            "allowed_tools": ["read_file", "write_file", "seal_candidate"],
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
    plan = _three_layer_plan(goal, profiles)
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
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine
    task_id = exec_lease["activity"]["task_id"]

    seed_mod.apply_reference_fix(run.executor_worktree)
    assert (run.executor_worktree / "tests_hidden").exists() is False
    assert run_pytest(run.executor_worktree, "tests/").returncode == 0

    step = client.post(
        f"/internal/v1/activities/{exec_lease['activity']['id']}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "three-layer seal",
            "tool_ref": "seal_candidate",
        },
        headers=exec_auth,
    )
    assert step.status_code == 201, step.text
    profile_ids = [profiles[layer] for layer in _LAYERS]
    input_blob = json.dumps(
        {
            "tool_ref": "seal_candidate",
            "tool_schema_digest": SEAL_CANDIDATE_SCHEMA_DIGEST,
            "parameters": {"verification_profile_ids": profile_ids},
        },
        separators=(",", ":"),
    ).encode()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e-three-layer-seal",
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
        headers=exec_auth,
    )
    assert prepared.status_code == 201, prepared.text
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=exec_auth,
        lease=exec_lease["lease"],
        effect=prepared.json()["data"],
        project_id=project_id,
        workspace_root=run.executor_worktree,
        allowed_paths=ALLOW,
        input_bytes=input_blob,
    )
    assert sealed.get("seal_error") is None, sealed.get("seal_error")
    candidate = sealed["candidate"]
    assert candidate is not None

    mat_root = run.artifacts / "auditor_materialized"
    if mat_root.exists():
        shutil.rmtree(mat_root)

    def read_bytes(digest: str) -> bytes:
        return store.read(project_id, digest)

    materialize_candidate_worktree(
        mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    attach_holdout_tests(mat_root, _HOLDOUT)
    assert run_pytest(mat_root, "tests/", "tests_hidden/").returncode == 0

    activity_now = client.get(
        f"/api/v1/activities/{exec_lease['activity']['id']}",
        headers=auth,
    ).json()["data"]
    assert (
        client.post(
            f"/internal/v1/activities/{exec_lease['activity']['id']}/outcomes",
            json={
                "lease": exec_lease["lease"],
                "expected_state_revision": activity_now["state_revision"],
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "evidence_ids": sealed["result_artifact_ids"],
                },
            },
            headers=exec_auth,
        ).status_code
        == 200
    )

    with create_engine(os.environ["RING_TEST_DATABASE_URL"]).begin() as db:
        audit_n = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:g AND kind='AUDIT' AND status='READY'"""
            ),
            {"g": goal["id"]},
        ).scalar_one()
        layers = list(
            db.execute(
                text(
                    """SELECT verification_assignments->0->>'layer' AS layer
                    FROM activities
                    WHERE goal_id=:g AND kind='AUDIT' AND status='READY'
                    ORDER BY layer"""
                ),
                {"g": goal["id"]},
            ).scalars()
        )
    assert audit_n == 3
    assert sorted(layers) == sorted(_LAYERS)

    # 缺层不得 Task DONE：逐层 PASS，前两次后仍非 DONE
    passed_layers: set[str] = set()
    for round_i in range(3):
        layer = _pass_next_audit(
            client=client,
            token=token,
            auth=auth,
            goal_id=goal["id"],
            store=store,
            engine=engine,
            project_id=project_id,
            candidate=candidate,
        )
        assert layer in _LAYERS
        assert layer not in passed_layers
        passed_layers.add(layer)
        task_now = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
        if round_i < 2:
            assert task_now["status"] != "DONE"
        assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
            "status"
        ] != "DONE"

    assert passed_layers == set(_LAYERS)
    task_done = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task_done["status"] == "DONE"
    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"

    summary = {
        "phase": "E2E-three-layer-audit",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "task_id": task_id,
        "layers": sorted(passed_layers),
        "profile_ids": profiles,
        "task_status": task_done["status"],
        "marks_goal_done": False,
        "non_goals": [
            "official_loop_autonomous",
            "integrate_finalize",
            "goal_done",
            "live_semantic_rubric_scoring",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
