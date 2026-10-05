"""E2E-3 PASS 缝：修复候选 + 物化 Auditor 树 + AUDIT PASS ≠ Goal DONE。

- Executor 写入修复并封存；从对象仓物化候选 + holdout 隐藏测 → 绿
- 异 worker AUDIT PASS → Task 可为 DONE，Goal 仍非 DONE
不宣称完整 profile 分层 / INTEGRATE/FINALIZE / Goal DONE。
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
from e2e3_order_helpers import ALLOW, boot_execute_for_seal, run_pytest
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


def test_e2e3_fixed_candidate_auditor_pass_not_goal_done(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_seal(api, objects, tmp_path, run_id="e2e3-pass-fixed")
    run = ctx["run"]
    seed = ctx["seed"]
    client, auth, goal = ctx["client"], ctx["auth"], ctx["goal"]

    # 仅在 Executor 修复；Auditor 输入改由封存后物化（非 apply_reference_fix 模拟）
    seed.apply_reference_fix(run.executor_worktree)
    assert (run.executor_worktree / "tests_hidden").exists() is False
    executor_public = run_pytest(run.executor_worktree, "tests/")
    assert executor_public.returncode == 0, executor_public.stdout + executor_public.stderr

    # 封存修复后候选
    step = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/steps",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "predecessor_step_id": None,
            "purpose": "e2e3 seal fixed",
            "tool_ref": "seal_candidate",
        },
        headers=ctx["exec_auth"],
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "seal_candidate",
            "tool_schema_digest": SEAL_CANDIDATE_SCHEMA_DIGEST,
            "parameters": {"verification_profile_ids": [ctx["profile_id"]]},
        },
        separators=(",", ":"),
    ).encode()
    input_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
        ctx["project_id"],
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e3-seal-fixed",
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
    assert all(not f["path"].startswith("tests_hidden/") for f in candidate["files"])

    # 物化封存候选 + 挂 holdout（非候选）；取代对 auditor_worktree 的 apply_reference_fix
    mat_root = run.artifacts / "auditor_materialized"
    if mat_root.exists():
        shutil.rmtree(mat_root)

    def read_bytes(digest: str) -> bytes:
        return ctx["store"].read(UUID(str(ctx["project_id"])), digest)

    materialize_candidate_worktree(
        mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    attach_holdout_tests(mat_root, _HOLDOUT)
    auditor_green = run_pytest(mat_root, "tests/", "tests_hidden/")
    assert auditor_green.returncode == 0, auditor_green.stdout + auditor_green.stderr

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

    audit_subject = str(uuid4())
    assert audit_subject != ctx["exec_subject"]
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
        assessment_verdict = db.execute(
            text("SELECT verdict FROM verification_assessments WHERE run_id=:id"),
            {"id": pass_run_id},
        ).scalar_one()
    assert assessment_verdict == "PASS"

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
                    "reason": "E2E-3 auditor pass on fixed candidate",
                },
            },
        },
        headers=audit_auth,
    )
    assert pass_outcome.status_code == 200, pass_outcome.text
    assert pass_outcome.json()["data"]["status"] == "SUCCEEDED"

    # Task 可 DONE；Goal 绝不能 DONE（E2E-4 前）
    task_after = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task_after["status"] == "DONE"
    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"

    summary = {
        "phase": "E2E-3-pass-fixed",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "candidate_manifest_id": candidate["id"],
        "exec_subject": ctx["exec_subject"],
        "audit_subject": audit_subject,
        "auditor_pytest_exit": auditor_green.returncode,
        "assessment_verdict": "PASS",
        "task_status": task_after["status"],
        "marks_goal_done": False,
        "non_goals": [
            "full_profile_layers",
            "integrate_finalize_release",
            "goal_done",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
