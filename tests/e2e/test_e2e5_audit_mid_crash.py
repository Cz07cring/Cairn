"""E2E-5 注入点 5：VerificationRun 登记后、AUDIT outcome 前提交失败。

obligation/审计层不得凭空变成 Task PASS/FAIL；Goal ≠ DONE。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from e2e3_order_helpers import ALLOW, boot_execute_for_seal, run_pytest
from e2e_tool_helpers import prepare_tool_effect
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_seal_candidate_effect,
)
from sqlalchemy import create_engine, text
from test_claims import _register_worker
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)

_ROOT = Path(__file__).resolve().parents[2]
_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"
_CRASH_ROUNDS = 10


def _force_expire(engine, attempt_id: str) -> None:
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )


def _trigger_expire_scan(client, token) -> None:
    probe = str(uuid4())
    _register_worker(probe, kinds=("PLAN", "AUDIT", "EXECUTE"))
    client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(probe, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )


def test_e2e5_audit_run_registered_before_outcome_crash_x10(api, objects, tmp_path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_seal(
        api,
        objects,
        tmp_path,
        run_id="e2e5-audit-mid",
        allowed_tools=["read_file", "write_file", "run_tests", "seal_candidate"],
    )
    client = ctx["client"]
    token = ctx["token"]
    auth = ctx["auth"]
    goal = ctx["goal"]
    exec_auth = ctx["exec_auth"]
    exec_lease = ctx["exec_lease"]
    project_id = ctx["project_id"]
    store = ctx["store"]
    run = ctx["run"]
    seed = ctx["seed"]
    ws = run.executor_worktree
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    client.app.state.objects = store

    seed.apply_reference_fix(ws)
    _step, effect, blob = prepare_tool_effect(
        {
            "client": client,
            "engine": engine,
            "store": store,
            "project_id": project_id,
            "exec_lease": exec_lease,
            "exec_auth": exec_auth,
        },
        tool_ref="seal_candidate",
        purpose="e2e5 seal for audit crash",
        parameters={"verification_profile_ids": [ctx["profile_id"]]},
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        predecessor_step_id=None,
        producer="e2e5-audit",
    )
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=exec_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=ws,
        allowed_paths=ALLOW,
        input_bytes=blob,
    )
    assert sealed.get("seal_error") is None, sealed.get("seal_error")
    candidate = sealed["candidate"]
    assert candidate is not None

    mat_root = run.artifacts / "auditor_materialized"
    if mat_root.exists():
        shutil.rmtree(mat_root)

    def read_bytes(digest: str) -> bytes:
        return store.read(UUID(str(project_id)), digest)

    materialize_candidate_worktree(
        mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    attach_holdout_tests(mat_root, _HOLDOUT)
    assert run_pytest(mat_root, "tests/", "tests_hidden/").returncode == 0

    activity_now = client.get(
        f"/api/v1/activities/{exec_lease['activity']['id']}",
        headers=auth,
    ).json()["data"]
    outcome = client.post(
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
    )
    assert outcome.status_code == 200, outcome.text
    task_id = exec_lease["activity"]["task_id"]

    audit_subject = str(uuid4())
    _register_worker(audit_subject, kinds=("AUDIT",))
    audit_auth = {"Authorization": "Bearer " + token(audit_subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**audit_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    audit_lease = claimed.json()["data"]
    audit_activity_id = audit_lease["activity"]["id"]
    audit_attempt_id = audit_lease["lease"]["attempt_id"]
    assignment = audit_lease["activity"]["verification_assignments"][0]
    binding = audit_lease["activity"]["binding"]

    with engine.begin() as db:
        obl_open = (
            db.execute(
                text(
                    """SELECT status FROM verification_obligations
                    WHERE activity_id=:a AND attempt_id=:t"""
                ),
                {"a": audit_activity_id, "t": audit_attempt_id},
            )
            .mappings()
            .one()
        )
    assert obl_open["status"] == "OPEN"

    receipt_id = receipt_artifact(engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run_id = post_verification_run(
        client,
        audit_auth,
        audit_lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        engine=engine,
    )

    with engine.begin() as db:
        obl_mid = (
            db.execute(
                text(
                    """SELECT status, assessment_id FROM verification_obligations
                    WHERE activity_id=:a AND attempt_id=:t"""
                ),
                {"a": audit_activity_id, "t": audit_attempt_id},
            )
            .mappings()
            .one()
        )
    assert obl_mid["status"] == "ASSESSED"
    assert obl_mid["assessment_id"] is not None

    # Audit run 已登记，但尚未提交 AUDIT activity outcome → 杀 Auditor
    _force_expire(engine, audit_attempt_id)
    _trigger_expire_scan(client, token)

    audit_act = client.get(
        f"/api/v1/activities/{audit_activity_id}", headers=auth
    ).json()["data"]
    assert audit_act["status"] != "SUCCEEDED"
    task_mid = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task_mid["status"] != "DONE"
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"

    for round_i in range(_CRASH_ROUNDS):
        # 旧租约不得提交 PASS/FAIL outcome
        forged = client.post(
            f"/internal/v1/activities/{audit_activity_id}/outcomes",
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
                        "verifier_run_ids": [run_id],
                        "verdict": "PASS" if round_i % 2 == 0 else "FAIL",
                        "criterion_results": [
                            {
                                "criterion_id": "A1",
                                "verdict": "PASS" if round_i % 2 == 0 else "FAIL",
                                "evidence_ids": [],
                                "reason": f"forged after crash {round_i}",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": f"e2e5 forged audit {round_i}",
                    },
                },
            },
            headers=audit_auth,
        )
        assert forged.status_code in (400, 409, 422), forged.text

        with engine.begin() as db:
            act_status = db.execute(
                text("SELECT status FROM activities WHERE id=:id"),
                {"id": audit_activity_id},
            ).scalar_one()
            task_status = db.execute(
                text("SELECT status FROM tasks WHERE id=:id"),
                {"id": task_id},
            ).scalar_one()
            obl_status = db.execute(
                text(
                    """SELECT status FROM verification_obligations
                    WHERE activity_id=:a AND attempt_id=:t"""
                ),
                {"a": audit_activity_id, "t": audit_attempt_id},
            ).scalar_one()
        assert act_status != "SUCCEEDED"
        assert task_status != "DONE"
        # obligation 未因伪造 outcome 推进到业务终态（Task/Goal DONE）
        assert obl_status in ("ASSESSED", "OPEN", "QUARANTINED", "SUPERSEDED")
        assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
            "status"
        ] != "DONE"

    summary = {
        "phase": "E2E-5-audit-run-before-outcome-crash",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "verification_run_id": run_id,
        "audit_activity_id": audit_activity_id,
        "crash_rounds": _CRASH_ROUNDS,
        "task_done": False,
        "marks_goal_done": False,
        "non_goals": [
            "e2e5_finalization_cas",
            "audit_pass_complete",
            "goal_done",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    engine.dispose()
