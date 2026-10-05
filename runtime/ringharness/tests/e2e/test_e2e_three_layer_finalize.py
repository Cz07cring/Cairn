"""E2E：订单三层 AUDIT 全 PASS → INTEGRATE → FINALIZE → Goal DONE。

DONE 仅经 Kernel + GLOBAL VerificationProfile + FinalizationBarrier + 唯一 ReleaseManifest。
Task 须 MECHANICAL+SEMANTIC+ADVERSARIAL 全过；缺层不得进 FINALIZE。
≠ 官方 Loop 自主 / live 诊断。
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
from evidence_ledger.objects import S3Objects
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_seal_candidate_effect,
)
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_e2e_three_layer_audit import (
    _LAYERS,
    _pass_next_audit,
    _seed_layer_profile,
    _three_layer_plan,
)
from test_finalization import _ready_project_with_global
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)

_ROOT = Path(__file__).resolve().parents[2]
_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"
_SCRIPTS = _ROOT / "scripts"


def test_e2e_order_three_layer_audit_then_finalize_goal_done(
    api, objects, tmp_path: Path
):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    import sys

    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import seed_business_e2e as seed

    run = seed.seed_run(run_id="e2e-three-layer-finalize", runtime_root=tmp_path)
    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)

    client, token, auth, _project, goal_body, _mechanical_id, global_profile_id = (
        _ready_project_with_global(api, objects)
    )
    project_id_str = goal_body["project_id"]
    profiles = {
        layer: _seed_layer_profile(client, auth, project_id_str, layer=layer)
        for layer in _LAYERS
    }
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": project_id_str,
            "name": "e2e-three-layer-finalize",
            "allowed_tools": [
                "read_file",
                "write_file",
                "run_tests",
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
    _drain_ready(
        client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE", "PLAN")
    )
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    assert goal["contract"]["success_criteria"][0]["verification_profile_id"] == (
        global_profile_id
    )

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

    seed.apply_reference_fix(run.executor_worktree)
    assert run_pytest(run.executor_worktree, "tests/").returncode == 0

    step = client.post(
        f"/internal/v1/activities/{exec_lease['activity']['id']}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "three-layer-finalize seal",
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
        producer_identity="test:e2e-three-layer-finalize-seal",
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
    assert audit_n == 3

    # 三层全 PASS → Task DONE；Goal 仍非 DONE
    passed_layers: set[str] = set()
    for _ in range(3):
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
        passed_layers.add(layer)
        assert (
            client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
                "status"
            ]
            != "DONE"
        )
    assert passed_layers == set(_LAYERS)
    task_done = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task_done["status"] == "DONE"
    goal_mid = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_mid["status"] == "RUNNING"
    assert goal_mid["barrier"] is None

    # INTEGRATE → 开最终屏障
    integ_subject = str(uuid4())
    _register_worker(integ_subject, kinds=("INTEGRATE", "FINALIZE"))
    integ_auth = {"Authorization": "Bearer " + token(integ_subject, ["worker"])}
    with create_engine(os.environ["RING_TEST_DATABASE_URL"]).begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='INTEGRATE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    int_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["INTEGRATE"], "capabilities": []},
        headers={**integ_auth, "Idempotency-Key": str(uuid4())},
    )
    assert int_claim.status_code == 200, int_claim.text
    integ = int_claim.json()["data"]
    assert integ["activity"]["kind"] == "INTEGRATE"
    assert integ["activity"]["goal_id"] == goal["id"]
    integration_commit = candidate.get("git_commit") or ("b" * 40)
    integrated = client.post(
        f"/internal/v1/activities/{integ['activity']['id']}/outcomes",
        json={
            "lease": integ["lease"],
            "expected_state_revision": integ["activity"]["state_revision"],
            "outcome": {
                "candidate_manifest_id": candidate["id"],
                "integration_commit": integration_commit,
                "evidence_ids": [],
            },
        },
        headers=integ_auth,
    )
    assert integrated.status_code == 200, integrated.text
    goal_verifying = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()[
        "data"
    ]
    assert goal_verifying["status"] == "VERIFYING"
    assert goal_verifying["barrier"]["status"] == "SEALED"
    barrier_id = goal_verifying["barrier"]["id"]

    # FINALIZE GLOBAL PASS → Goal DONE + 唯一 ReleaseManifest
    with create_engine(os.environ["RING_TEST_DATABASE_URL"]).begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='FINALIZE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    fin_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["FINALIZE"], "capabilities": []},
        headers={**integ_auth, "Idempotency-Key": str(uuid4())},
    )
    assert fin_claim.status_code == 200, fin_claim.text
    fin = fin_claim.json()["data"]
    assert fin["activity"]["kind"] == "FINALIZE"
    assert fin["activity"]["goal_id"] == goal["id"]
    fin_assign = fin["activity"]["verification_assignments"][0]
    assert fin_assign["layer"] == "GLOBAL"
    assert fin_assign["verification_profile_id"] == global_profile_id
    fin_receipt = receipt_artifact(engine, store, project_id)
    fin_verifier = profile_verifier_digest(
        client, auth, str(project_id), fin_assign["verification_profile_id"]
    )
    fin_run_id = post_verification_run(
        client,
        integ_auth,
        fin,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=fin_verifier,
        receipt_id=fin_receipt,
        criterion_id="C1",
    )
    finalized = client.post(
        f"/internal/v1/activities/{fin['activity']['id']}/outcomes",
        json={
            "lease": fin["lease"],
            "expected_state_revision": fin["activity"]["state_revision"],
            "outcome": {
                "barrier_id": barrier_id,
                "candidate_manifest_id": candidate["id"],
                "global_audits": [
                    {
                        "subject_candidate_manifest_id": candidate["id"],
                        "goal_contract_revision": goal["contract_revision"],
                        "task_contract_revision": None,
                        "verification_profile_id": fin_assign[
                            "verification_profile_id"
                        ],
                        "layer": "GLOBAL",
                        "audit_round": fin_assign["audit_round"],
                        "verifier_run_ids": [fin_run_id],
                        "verdict": "PASS",
                        "criterion_results": [
                            {
                                "criterion_id": "C1",
                                "verdict": "PASS",
                                "evidence_ids": [],
                                "reason": "三层 AUDIT 后全局验收通过",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "E2E three-layer finalize",
                    }
                ],
                "evidence_ids": [],
            },
        },
        headers=integ_auth,
    )
    assert finalized.status_code == 200, finalized.text

    goal_done = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_done["status"] == "DONE"
    assert goal_done["barrier"]["status"] == "RELEASED"
    assert goal_done["release_manifest_id"] is not None

    release = client.get(f"/api/v1/goals/{goal['id']}/release", headers=auth)
    assert release.status_code == 200, release.text
    body = release.json()["data"]
    assert body["validity"]["status"] == "VALID"
    assert body["manifest"]["candidate_manifest_id"] == candidate["id"]

    with create_engine(os.environ["RING_TEST_DATABASE_URL"]).begin() as db:
        release_n = db.execute(
            text(
                """SELECT count(*) FROM release_manifests
                WHERE goal_id=:g"""
            ),
            {"g": goal["id"]},
        ).scalar_one()
    assert release_n == 1

    summary = {
        "phase": "E2E-three-layer-finalize",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "task_id": task_id,
        "layers": sorted(passed_layers),
        "profile_ids": profiles,
        "global_profile_id": global_profile_id,
        "task_status": "DONE",
        "goal_status": "DONE",
        "barrier_status": "RELEASED",
        "release_manifest_id": goal_done["release_manifest_id"],
        "release_count": 1,
        "done_authority": "control_kernel_finalization",
        "non_goals": [
            "official_loop_autonomous",
            "live_diagnose_cycle",
            "live_semantic_rubric_scoring",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
