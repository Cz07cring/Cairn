"""E2E-3 首缝：Auditor 工作区隔离 + 隐藏测复跑红 + AUDIT FAIL ≠ Goal/Task DONE。

- Executor worktree 无 tests_hidden；Auditor 有独立 worktree + 隐藏测
- Auditor 在 buggy 候选上跑 public+hidden → 红（机械失败证据）
- 不同 worker 领 AUDIT；Assessment/Audit FAIL 后 Goal/Task 仍非 DONE
不宣称 E2E-3 全完成条件；不宣称 Goal DONE。
"""

from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from e2e3_order_helpers import ALLOW, boot_execute_for_seal, run_pytest
from execution_broker import run_seal_candidate_effect
from sqlalchemy import text
from test_claims import _register_worker
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)


def test_e2e3_auditor_isolation_and_fail_not_done(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_seal(api, objects, tmp_path, run_id="e2e3-isolation-fail")
    run = ctx["run"]
    client, auth, goal = ctx["client"], ctx["auth"], ctx["goal"]

    # —— 隔离：身份与 workspace ——
    assert run.executor_worktree.resolve() != run.auditor_worktree.resolve()
    assert (run.executor_worktree / "tests_hidden").exists() is False
    assert (run.auditor_worktree / "tests_hidden").is_dir()
    assert (run.auditor_worktree / "tests_hidden" / "test_idempotency_concurrent.py").is_file()

    # Auditor 在 buggy 树上跑公开+隐藏 → 红（独立复跑，非 Executor 自报）
    auditor_red = run_pytest(run.auditor_worktree, "tests/", "tests_hidden/")
    assert auditor_red.returncode != 0, auditor_red.stdout + auditor_red.stderr

    # Executor 侧公开红；且仍无隐藏测可跑
    executor_red = run_pytest(run.executor_worktree, "tests/")
    assert executor_red.returncode != 0
    assert list(run.executor_worktree.glob("tests_hidden/**/*.py")) == []

    # —— 封存 buggy 候选 → EXECUTE outcome → AUDIT READY ——
    step = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/steps",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "predecessor_step_id": None,
            "purpose": "e2e3 seal buggy",
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
        producer_identity="test:e2e3-seal",
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
    task = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task["status"] == "VERIFYING"

    # —— 不同 Auditor worker 领 AUDIT ——
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
    assert audit_lease["activity"]["kind"] == "AUDIT"
    assert audit_lease["activity"]["target"]["id"] == candidate["id"]
    assignment = audit_lease["activity"]["verification_assignments"][0]
    binding = audit_lease["activity"]["binding"]

    receipt_id = receipt_artifact(ctx["engine"], ctx["store"], ctx["project_id"])
    verifier_digest = profile_verifier_digest(
        client, auth, str(ctx["project_id"]), assignment["verification_profile_id"]
    )
    fail_run_id = post_verification_run(
        client,
        audit_auth,
        audit_lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        checks_passed="false",
    )
    with ctx["engine"].connect() as db:
        assessment_verdict = db.execute(
            text("SELECT verdict FROM verification_assessments WHERE run_id=:id"),
            {"id": fail_run_id},
        ).scalar_one()
    assert assessment_verdict == "FAIL"

    fail_outcome = client.post(
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
                    "verifier_run_ids": [fail_run_id],
                    "verdict": "FAIL",
                    "criterion_results": [
                        {
                            "criterion_id": "A1",
                            "verdict": "FAIL",
                            "evidence_ids": [],
                            "reason": "公开+隐藏幂等测未通过",
                        }
                    ],
                    "evidence_ids": [],
                    "reason": "E2E-3 auditor fail on buggy candidate",
                },
            },
        },
        headers=audit_auth,
    )
    assert fail_outcome.status_code == 200, fail_outcome.text
    assert fail_outcome.json()["data"]["status"] == "SUCCEEDED"

    task_after = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task_after["status"] != "DONE"
    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"

    summary = {
        "phase": "E2E-3-isolation-fail",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "candidate_manifest_id": candidate["id"],
        "exec_subject": ctx["exec_subject"],
        "audit_subject": audit_subject,
        "executor_has_hidden": False,
        "auditor_has_hidden": True,
        "auditor_pytest_exit": auditor_red.returncode,
        "assessment_verdict": "FAIL",
        "marks_goal_done": False,
        "non_goals": [
            "full_e2e3_profile_layers",
            "materialize_candidate_copy_api",
            "goal_done",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
