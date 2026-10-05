"""E2E-4 业务全路径：Broker 改真实业务码 → 测绿 → seal → 审计 → FINALIZE → Goal DONE。

禁止 apply_reference_fix；修复正文来自 fixture reference_fix，经 write_file Effect 落地。
不宣称官方 AgentLoop 自主决策；AUDIT 经 Broker `run_tests(suite=auditor)`（VERIFICATION scope）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import (
    GIT_DIFF_SCHEMA_DIGEST,
    RUN_TESTS_SCHEMA_DIGEST,
    SEAL_CANDIDATE_SCHEMA_DIGEST,
    WRITE_FILE_SCHEMA_DIGEST,
)
from e2e3_order_helpers import ALLOW
from e2e4_order_helpers import boot_execute_for_finalize
from e2e_tool_helpers import load_reference_fix_store, prepare_tool_effect
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_git_diff_effect,
    run_run_tests_effect,
    run_seal_candidate_effect,
    run_write_file_effect,
)
from sqlalchemy import text
from test_claims import _register_worker
from verification_run_helpers import (
    post_verification_run_from_broker_tests,
    profile_verifier_digest,
)

_ROOT = Path(__file__).resolve().parents[2]
_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"


def test_e2e4_order_business_path_write_file_to_goal_done(api, objects, tmp_path: Path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_finalize(api, objects, tmp_path, run_id="e2e4-biz-path")
    run = ctx["run"]
    client, auth, goal = ctx["client"], ctx["auth"], ctx["goal"]
    lease = ctx["exec_lease"]["lease"]
    ws = run.executor_worktree
    assert (ws / "tests_hidden").exists() is False
    buggy = (ws / "order_service" / "store.py").read_text(encoding="utf-8")
    assert "_by_key" not in buggy

    trail: list[str] = []
    pred = None
    fix_body = load_reference_fix_store()
    assert "_by_key" in fix_body and "threading" in fix_body

    # 1) run_tests → 红（幂等缺陷）
    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="run_tests",
        purpose="biz public red",
        parameters={"suite": "public"},
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        predecessor_step_id=pred,
        producer="e2e4-biz",
    )
    pred = step["id"]
    red = run_run_tests_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        input_bytes=blob,
    )
    assert red["result"].exit_code != 0
    trail.append("run_tests_red")

    # 2) write_file → 真实业务修复正文
    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="write_file",
        purpose="biz fix store.py",
        parameters={"path": "order_service/store.py", "content": fix_body},
        schema_digest=WRITE_FILE_SCHEMA_DIGEST,
        predecessor_step_id=pred,
        producer="e2e4-biz",
    )
    pred = step["id"]
    wrote = run_write_file_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        allowed_paths=["order_service/**"],
        input_bytes=blob,
    )
    assert wrote["result"].observed_outcome == "SUCCEEDED", wrote["result"].error
    on_disk = (ws / "order_service" / "store.py").read_text(encoding="utf-8")
    assert on_disk == fix_body
    trail.append("write_file_reference_fix")

    # 3) run_tests → 绿
    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="run_tests",
        purpose="biz public green",
        parameters={"suite": "public"},
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        predecessor_step_id=pred,
        producer="e2e4-biz",
    )
    pred = step["id"]
    green = run_run_tests_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        input_bytes=blob,
    )
    assert green["result"].exit_code == 0, green["result"].stdout_text
    trail.append("run_tests_green")

    # 4) git_diff → dirty
    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="git_diff",
        purpose="biz diff",
        parameters={},
        schema_digest=GIT_DIFF_SCHEMA_DIGEST,
        predecessor_step_id=pred,
        producer="e2e4-biz",
    )
    pred = step["id"]
    diffed = run_git_diff_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        input_bytes=blob,
    )
    assert diffed["result"].dirty is True
    trail.append("git_diff")

    # 5) seal
    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="seal_candidate",
        purpose="biz seal",
        parameters={"verification_profile_ids": [ctx["mechanical_id"]]},
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        predecessor_step_id=pred,
        producer="e2e4-biz",
    )
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=lease,
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=ws,
        allowed_paths=ALLOW,
        input_bytes=blob,
    )
    assert sealed.get("seal_error") is None, sealed.get("seal_error")
    candidate = sealed["candidate"]
    assert candidate is not None
    trail.append("seal_candidate")

    # 物化 + holdout（隐藏测不在候选内）
    mat_root = run.artifacts / "auditor_materialized"
    if mat_root.exists():
        shutil.rmtree(mat_root)

    def read_bytes(digest: str) -> bytes:
        return ctx["store"].read(UUID(str(ctx["project_id"])), digest)

    materialize_candidate_worktree(
        mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    assert (mat_root / "tests_hidden").exists() is False
    attach_holdout_tests(mat_root, _HOLDOUT)
    trail.append("materialize_holdout")

    activity_now = client.get(
        f"/api/v1/activities/{ctx['exec_lease']['activity']['id']}",
        headers=auth,
    ).json()["data"]
    outcome = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/outcomes",
        json={
            "lease": lease,
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

    # AUDIT：经 Broker run_tests(suite=auditor) 在物化树上跑测（VERIFICATION scope）
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
    ctx["audit_lease"] = audit_lease
    ctx["audit_auth"] = audit_auth
    assignment = audit_lease["activity"]["verification_assignments"][0]
    binding = audit_lease["activity"]["binding"]

    # 拒 write_file 于 AUDIT
    denied = client.post(
        f"/internal/v1/activities/{audit_lease['activity']['id']}/steps",
        json={
            "lease": audit_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "must deny write",
            "tool_ref": "write_file",
        },
        headers=audit_auth,
    )
    assert denied.status_code == 422, denied.text

    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="run_tests",
        purpose="auditor suite on materialized tree",
        parameters={"suite": "auditor"},
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        predecessor_step_id=None,
        producer="e2e4-biz-audit",
        lease_key="audit_lease",
        auth_key="audit_auth",
    )
    assert effect["scope"] == "VERIFICATION"
    audited = run_run_tests_effect(
        client,
        worker_auth=audit_auth,
        lease=audit_lease["lease"],
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=mat_root,
        input_bytes=blob,
    )
    assert audited["result"].observed_outcome == "SUCCEEDED"
    assert audited["result"].exit_code == 0, audited["result"].stdout_text
    assert audited["result"].suite == "auditor"
    trail.append("broker_auditor_run_tests")

    verifier_digest = profile_verifier_digest(
        client, auth, str(ctx["project_id"]), assignment["verification_profile_id"]
    )
    pass_run_id, audit_checks = post_verification_run_from_broker_tests(
        client,
        audit_auth,
        audit_lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        broker_ran=audited,
        engine=ctx["engine"],
        store=ctx["store"],
        project_id=ctx["project_id"],
    )
    assert audit_checks == "true"
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
                            "reason": "Broker auditor suite 公开+隐藏测已绿",
                        }
                    ],
                    "evidence_ids": [],
                    "reason": "E2E-4 business path auditor pass via Broker",
                },
            },
        },
        headers=audit_auth,
    )
    assert pass_outcome.status_code == 200, pass_outcome.text
    assert client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]["status"] == "DONE"
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]["status"] == (
        "RUNNING"
    )
    trail.append("audit_pass_task_done")

    # INTEGRATE
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
    barrier_id = goal_verifying["barrier"]["id"]
    trail.append("integrate")

    # FINALIZE → Broker auditor suite → Goal DONE
    fin_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["FINALIZE"], "capabilities": []},
        headers={**integ_auth, "Idempotency-Key": str(uuid4())},
    )
    assert fin_claim.status_code == 200, fin_claim.text
    fin = fin_claim.json()["data"]
    fin_assign = fin["activity"]["verification_assignments"][0]
    ctx["fin_lease"] = fin
    ctx["fin_auth"] = integ_auth
    step, effect, blob = prepare_tool_effect(
        ctx,
        tool_ref="run_tests",
        purpose="finalize global auditor suite",
        parameters={"suite": "auditor"},
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        predecessor_step_id=None,
        producer="e2e4-biz-finalize",
        lease_key="fin_lease",
        auth_key="fin_auth",
    )
    assert effect["scope"] == "VERIFICATION"
    finalized_tests = run_run_tests_effect(
        client,
        worker_auth=integ_auth,
        lease=fin["lease"],
        effect=effect,
        project_id=ctx["project_id"],
        workspace_root=mat_root,
        input_bytes=blob,
    )
    assert finalized_tests["result"].exit_code == 0, finalized_tests["result"].stdout_text
    trail.append("broker_finalize_run_tests")
    fin_run_id, fin_checks = post_verification_run_from_broker_tests(
        client,
        integ_auth,
        fin,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=profile_verifier_digest(
            client, auth, str(ctx["project_id"]), fin_assign["verification_profile_id"]
        ),
        broker_ran=finalized_tests,
        engine=ctx["engine"],
        store=ctx["store"],
        project_id=ctx["project_id"],
        criterion_id="C1",
    )
    assert fin_checks == "true"
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
                                "reason": "业务全路径全局验收",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "E2E-4 business path finalize",
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
    release = client.get(f"/api/v1/goals/{goal['id']}/release", headers=auth).json()["data"]
    assert release["validity"]["status"] == "VALID"
    trail.append("finalize_goal_done")

    summary = {
        "phase": "E2E-4-business-path",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "candidate_manifest_id": candidate["id"],
        "trail": trail,
        "fix_via": "write_file_effect+reference_fix/store.py",
        "used_apply_reference_fix": False,
        "auditor_via": "broker_run_tests_suite_auditor",
        "audit_effect_scope": "VERIFICATION",
        "audit_checks_from_pytest": audit_checks,
        "finalize_checks_from_pytest": fin_checks,
        "goal_status": "DONE",
        "release_validity": release["validity"]["status"],
        "done_authority": "control_kernel_finalization",
        "non_goals": [
            "official_agent_loop_autonomous_multistep",
            "e2e5_recovery_redteam",
            "full_profile_layers_mechanical_semantic_adversarial",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
