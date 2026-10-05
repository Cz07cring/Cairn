"""E2E-4 首缝：订单修复候选 → AUDIT PASS → INTEGRATE → FINALIZE → Goal DONE。

DONE 仅经 Kernel + GLOBAL VerificationProfile + FinalizationBarrier；
Runner/Broker 无直写 Goal DONE。不宣称完整 profile 分层 / E2E-5 恢复红队。
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
from e2e3_order_helpers import ALLOW
from e2e4_order_helpers import boot_execute_for_finalize, run_pytest
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_seal_candidate_effect,
)
from sqlalchemy import text
from test_claims import _register_worker
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)

_ROOT = Path(__file__).resolve().parents[2]
_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"


def test_e2e4_order_finalize_marks_goal_done(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_finalize(api, objects, tmp_path, run_id="e2e4-finalize")
    run = ctx["run"]
    seed = ctx["seed"]
    client, auth, goal = ctx["client"], ctx["auth"], ctx["goal"]

    seed.apply_reference_fix(run.executor_worktree)
    assert run_pytest(run.executor_worktree, "tests/").returncode == 0

    # —— seal 修复候选 ——
    step = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/steps",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "predecessor_step_id": None,
            "purpose": "e2e4 seal",
            "tool_ref": "seal_candidate",
        },
        headers=ctx["exec_auth"],
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "seal_candidate",
            "tool_schema_digest": SEAL_CANDIDATE_SCHEMA_DIGEST,
            "parameters": {"verification_profile_ids": [ctx["mechanical_id"]]},
        },
        separators=(",", ":"),
    ).encode()
    input_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
        ctx["project_id"],
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e4-seal",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "seal_candidate",
            "input_artifact_id": str(input_art.id),
        },
        headers=ctx["exec_auth"],
    )
    assert prepared.status_code == 201, prepared.text
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=ctx["exec_lease"]["lease"],
        effect=prepared.json()["data"],
        project_id=ctx["project_id"],
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
        return ctx["store"].read(UUID(str(ctx["project_id"])), digest)

    materialize_candidate_worktree(
        mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    attach_holdout_tests(mat_root, _HOLDOUT)
    assert run_pytest(mat_root, "tests/", "tests_hidden/").returncode == 0

    activity_now = client.get(
        f"/api/v1/activities/{ctx['exec_lease']['activity']['id']}",
        headers=auth,
    ).json()["data"]
    outcome = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/outcomes",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "expected_state_revision": activity_now["state_revision"],
            "outcome": {
                "candidate_manifest_id": candidate["id"],
                "evidence_ids": sealed["result_artifact_ids"],
            },
        },
        headers=ctx["exec_auth"],
    )
    assert outcome.status_code == 200, outcome.text
    task_id = ctx["exec_lease"]["activity"]["task_id"]

    # —— AUDIT PASS → Task DONE；此时 Goal 仍非 DONE ——
    audit_subject = str(uuid4())
    _register_worker(audit_subject, kinds=("AUDIT",))
    audit_auth = {"Authorization": "Bearer " + ctx["token"](audit_subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**audit_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    audit_lease = claimed.json()["data"]
    assignment = audit_lease["activity"]["verification_assignments"][0]
    binding = audit_lease["activity"]["binding"]
    receipt_id = receipt_artifact(ctx["engine"], ctx["store"], ctx["project_id"])
    verifier_digest = profile_verifier_digest(
        client, auth, str(ctx["project_id"]), assignment["verification_profile_id"]
    )
    pass_run_id = post_verification_run(
        client,
        audit_auth,
        audit_lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        checks_passed="true",
    )
    with ctx["engine"].connect() as db:
        assert (
            db.execute(
                text("SELECT verdict FROM verification_assessments WHERE run_id=:id"),
                {"id": pass_run_id},
            ).scalar_one()
            == "PASS"
        )
    pass_outcome = client.post(
        f"/internal/v1/activities/{audit_lease['activity']['id']}/outcomes",
        json={
            "lease": audit_lease["lease"],
            "expected_state_revision": audit_lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "CANDIDATE",
                "audit": {
                    "subject_candidate_manifest_id": candidate["id"],
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "task_contract_revision": binding["task_contract_revision"],
                    "verification_profile_id": assignment["verification_profile_id"],
                    "layer": assignment["layer"],
                    "audit_round": assignment["audit_round"],
                    "verifier_run_ids": [pass_run_id],
                    "verdict": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": "A1",
                            "verdict": "PASS",
                            "evidence_ids": [],
                            "reason": "公开+隐藏幂等测通过",
                        }
                    ],
                    "evidence_ids": [],
                    "reason": "E2E-4 auditor pass",
                },
            },
        },
        headers=audit_auth,
    )
    assert pass_outcome.status_code == 200, pass_outcome.text
    task_after = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task_after["status"] == "DONE"
    goal_mid = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_mid["status"] == "RUNNING"
    assert goal_mid["barrier"] is None

    # —— INTEGRATE → 开最终屏障 ——
    integ_subject = str(uuid4())
    _register_worker(integ_subject, kinds=("INTEGRATE", "FINALIZE"))
    integ_auth = {"Authorization": "Bearer " + ctx["token"](integ_subject, ["worker"])}
    int_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["INTEGRATE"], "capabilities": []},
        headers={**integ_auth, "Idempotency-Key": str(uuid4())},
    )
    assert int_claim.status_code == 200, int_claim.text
    integ = int_claim.json()["data"]
    assert integ["activity"]["kind"] == "INTEGRATE"
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
    goal_verifying = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_verifying["status"] == "VERIFYING"
    assert goal_verifying["barrier"]["status"] == "SEALED"
    barrier_id = goal_verifying["barrier"]["id"]

    # —— FINALIZE GLOBAL PASS → Goal DONE + Release ——
    fin_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["FINALIZE"], "capabilities": []},
        headers={**integ_auth, "Idempotency-Key": str(uuid4())},
    )
    assert fin_claim.status_code == 200, fin_claim.text
    fin = fin_claim.json()["data"]
    assert fin["activity"]["kind"] == "FINALIZE"
    fin_assign = fin["activity"]["verification_assignments"][0]
    assert fin_assign["layer"] == "GLOBAL"
    fin_receipt = receipt_artifact(ctx["engine"], ctx["store"], ctx["project_id"])
    fin_verifier = profile_verifier_digest(
        client, auth, str(ctx["project_id"]), fin_assign["verification_profile_id"]
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
                        "verification_profile_id": fin_assign["verification_profile_id"],
                        "layer": "GLOBAL",
                        "audit_round": fin_assign["audit_round"],
                        "verifier_run_ids": [fin_run_id],
                        "verdict": "PASS",
                        "criterion_results": [
                            {
                                "criterion_id": "C1",
                                "verdict": "PASS",
                                "evidence_ids": [],
                                "reason": "订单幂等全局验收通过",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "E2E-4 finalization pass",
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

    summary = {
        "phase": "E2E-4-finalize",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "candidate_manifest_id": candidate["id"],
        "task_status": "DONE",
        "goal_status": "DONE",
        "barrier_status": "RELEASED",
        "release_manifest_id": goal_done["release_manifest_id"],
        "release_validity": body["validity"]["status"],
        "done_authority": "control_kernel_finalization",
        "non_goals": [
            "full_profile_layers_mechanical_semantic_adversarial",
            "e2e5_recovery_redteam",
            "live_official_agent_loop_full_chain",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
