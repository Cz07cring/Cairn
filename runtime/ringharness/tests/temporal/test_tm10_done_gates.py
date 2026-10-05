"""TM10 部分举证：缺 run / 伪 producer / Assessment 失败 / 义务 OPEN → 不能 DONE。

真实 PG；复用 AUDIT/FINALIZE STRICT `verifier_run_ids` 与义务门禁（LEGACY Kernel 权威亦计）。
非完整 AT04；不以 Workflow 冒充 Goal/Task DONE。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import text
from test_audits import _seal_and_finish_execute
from test_claims import _register_worker
from test_finalization import _claim_finalize_lease
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)


def _claim_audit(client, token, candidate):
    """领取 AUDIT lease（候选须已封存并完成 EXECUTE）。"""
    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["kind"] == "AUDIT"
    assert lease["activity"]["target"]["id"] == candidate["id"]
    return worker_auth, lease


def _audit_outcome_body(lease, candidate, *, run_ids, verdict: str, criterion_verdict: str):
    assignment = lease["activity"]["verification_assignments"][0]
    binding = lease["activity"]["binding"]
    return {
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
                "verifier_run_ids": run_ids,
                "verdict": verdict,
                "criterion_results": [
                    {
                        "criterion_id": "A1",
                        "verdict": criterion_verdict,
                        "evidence_ids": [],
                        "reason": f"TM10 {verdict}",
                    }
                ],
                "evidence_ids": [],
                "reason": f"TM10 audit {verdict}",
            },
        },
    }


def test_tm10_audit_rejects_unknown_verifier_run_id(api, objects):
    """缺 VerificationRun：AUDIT PASS outcome 被 STRICT 拒绝，Task 不能 DONE。"""
    client, token, auth, _goal, candidate, task_id = _seal_and_finish_execute(api, objects)
    worker_auth, lease = _claim_audit(client, token, candidate)
    outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json=_audit_outcome_body(
            lease,
            candidate,
            run_ids=[str(uuid4())],
            verdict="PASS",
            criterion_verdict="PASS",
        ),
        headers=worker_auth,
    )
    assert outcome.status_code == 422, outcome.text
    assert outcome.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "VerificationRun" in outcome.json()["error"]["message"]
    task = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task["status"] != "DONE"


def test_tm10_finalize_rejects_unknown_verifier_run_id(api, objects):
    """缺 VerificationRun：FINALIZE PASS outcome 被 STRICT 拒绝，Goal 不能 DONE。"""
    client, _token, auth, goal, candidate, worker_auth, fin, barrier_id, _store = (
        _claim_finalize_lease(api, objects)
    )
    fin_assign = fin["activity"]["verification_assignments"][0]
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
                        "verifier_run_ids": [str(uuid4())],
                        "verdict": "PASS",
                        "criterion_results": [
                            {
                                "criterion_id": "C1",
                                "verdict": "PASS",
                                "evidence_ids": [],
                                "reason": "伪全局通过",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "TM10 缺 run",
                    }
                ],
                "evidence_ids": [],
            },
        },
        headers=worker_auth,
    )
    assert finalized.status_code == 422, finalized.text
    assert finalized.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "VerificationRun" in finalized.json()["error"]["message"]
    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"


def test_tm10_audit_rejects_foreign_producer_run(api, objects):
    """伪 producer：引用另一 AUDIT 活动登记的 VerificationRun → STRICT 拒绝。"""
    store, _, _ = objects
    # 活动 A：登记合法 run
    client_a, token_a, auth_a, _goal_a, candidate_a, _task_a = _seal_and_finish_execute(
        api, objects
    )
    client_a.app.state.objects = store
    worker_a, lease_a = _claim_audit(client_a, token_a, candidate_a)
    project_a = UUID(lease_a["activity"]["project_id"])
    assignment_a = lease_a["activity"]["verification_assignments"][0]
    receipt_a = receipt_artifact(client_a.app.state.engine, store, project_a)
    digest_a = profile_verifier_digest(
        client_a, auth_a, str(project_a), assignment_a["verification_profile_id"]
    )
    foreign_run_id = post_verification_run(
        client_a,
        worker_a,
        lease_a,
        subject_id=candidate_a["id"],
        subject_digest=candidate_a["content_digest"],
        verifier_digest=digest_a,
        receipt_id=receipt_a,
    )

    # 活动 B：outcome 引用 A 的 run_id（producer 不一致）
    client_b, token_b, auth_b, _goal_b, candidate_b, task_b = _seal_and_finish_execute(
        api, objects
    )
    client_b.app.state.objects = store
    worker_b, lease_b = _claim_audit(client_b, token_b, candidate_b)
    outcome = client_b.post(
        f"/internal/v1/activities/{lease_b['activity']['id']}/outcomes",
        json=_audit_outcome_body(
            lease_b,
            candidate_b,
            run_ids=[foreign_run_id],
            verdict="PASS",
            criterion_verdict="PASS",
        ),
        headers=worker_b,
    )
    assert outcome.status_code == 422, outcome.text
    assert outcome.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "活动" in outcome.json()["error"]["message"]
    task = client_b.get(f"/api/v1/tasks/{task_b}", headers=auth_b).json()["data"]
    assert task["status"] != "DONE"


def test_tm10_open_obligation_blocks_task_done(api, objects):
    """义务仍 OPEN：即便 Audit 宣称 PASS，Task DONE 门禁拒绝。"""
    store, _, _ = objects
    client, token, auth, _goal, candidate, task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    activity_id = UUID(lease["activity"]["id"])
    attempt_id = UUID(lease["lease"]["attempt_id"])
    assignment = lease["activity"]["verification_assignments"][0]

    with engine.connect() as db:
        status = db.execute(
            text(
                """SELECT status FROM verification_obligations
                WHERE activity_id=:activity AND attempt_id=:attempt"""
            ),
            {"activity": activity_id, "attempt": attempt_id},
        ).scalar_one()
    assert status == "OPEN"

    receipt_id = receipt_artifact(engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
    )
    # 结算后再强制 OPEN：举证 DONE 前 assert_no_pending_obligations
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE verification_obligations
                SET status='OPEN', assessment_id=NULL, updated_at=clock_timestamp()
                WHERE activity_id=:activity AND attempt_id=:attempt"""
            ),
            {"activity": activity_id, "attempt": attempt_id},
        )

    outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json=_audit_outcome_body(
            lease,
            candidate,
            run_ids=[run_id],
            verdict="PASS",
            criterion_verdict="PASS",
        ),
        headers=worker_auth,
    )
    assert outcome.status_code == 422, outcome.text
    assert outcome.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "VerificationObligation" in outcome.json()["error"]["message"]
    task = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task["status"] != "DONE"


def test_tm10_assessment_and_audit_fail_do_not_mark_task_done(api, objects):
    """Assessment FAIL + Audit FAIL：活动可 SUCCEEDED，但 Task/Goal 不能 DONE。"""
    store, _, _ = objects
    client, token, auth, goal, candidate, task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    project_id = UUID(lease["activity"]["project_id"])
    assignment = lease["activity"]["verification_assignments"][0]
    receipt_id = receipt_artifact(client.app.state.engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    fail_run_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        checks_passed="false",
    )
    with client.app.state.engine.connect() as db:
        assessment_verdict = db.execute(
            text("SELECT verdict FROM verification_assessments WHERE run_id=:id"),
            {"id": fail_run_id},
        ).scalar_one()
    assert assessment_verdict == "FAIL"

    fail_outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json=_audit_outcome_body(
            lease,
            candidate,
            run_ids=[fail_run_id],
            verdict="FAIL",
            criterion_verdict="FAIL",
        ),
        headers=worker_auth,
    )
    assert fail_outcome.status_code == 200, fail_outcome.text
    assert fail_outcome.json()["data"]["status"] == "SUCCEEDED"
    task_after_fail = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task_after_fail["status"] != "DONE"
    goal_after_fail = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after_fail["status"] != "DONE"


def test_tm10_audit_insufficient_does_not_mark_task_done(api, objects):
    """Audit INSUFFICIENT：不能冒充 Task DONE。"""
    store, _, _ = objects
    client, token, auth, _goal, candidate, task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    project_id = UUID(lease["activity"]["project_id"])
    assignment = lease["activity"]["verification_assignments"][0]
    receipt_id = receipt_artifact(client.app.state.engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
    )
    outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json=_audit_outcome_body(
            lease,
            candidate,
            run_ids=[run_id],
            verdict="INSUFFICIENT",
            criterion_verdict="INSUFFICIENT",
        ),
        headers=worker_auth,
    )
    assert outcome.status_code == 200, outcome.text
    task = client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]
    assert task["status"] != "DONE"
