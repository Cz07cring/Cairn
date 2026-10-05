"""VerificationObligation：claim 开立 OPEN，VerificationRun 后 ASSESSED。"""

from uuid import UUID, uuid4

from sqlalchemy import text
from test_audits import _seal_and_finish_execute
from test_claims import _register_worker
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)


def _claim_audit(client, token, candidate):
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
    return worker_auth, lease


def test_obligation_open_on_claim_assessed_after_run(api, objects):
    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    activity_id = UUID(lease["activity"]["id"])
    attempt_id = UUID(lease["lease"]["attempt_id"])
    assignment = lease["activity"]["verification_assignments"][0]

    with engine.connect() as db:
        row = (
            db.execute(
                text(
                    """SELECT * FROM verification_obligations
                    WHERE activity_id=:activity AND attempt_id=:attempt"""
                ),
                {"activity": activity_id, "attempt": attempt_id},
            )
            .mappings()
            .one()
        )
    assert row["status"] == "OPEN"
    assert row["subject_type"] == "CANDIDATE"
    assert str(row["subject_id"]) == candidate["id"]
    assert str(row["profile_id"]) == assignment["verification_profile_id"]
    assert row["layer"] == assignment["layer"]
    assert int(row["audit_round"]) == int(assignment["audit_round"])
    assert list(row["effect_ids"] or []) == []
    assert list(row["invocation_ids"] or []) == []
    assert row["assessment_id"] is None

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

    with engine.connect() as db:
        settled = (
            db.execute(
                text(
                    """SELECT status, assessment_id FROM verification_obligations
                    WHERE activity_id=:activity AND attempt_id=:attempt"""
                ),
                {"activity": activity_id, "attempt": attempt_id},
            )
            .mappings()
            .one()
        )
        assessment_id = db.execute(
            text("SELECT id FROM verification_assessments WHERE run_id=:id"),
            {"id": run_id},
        ).scalar_one()
    assert settled["status"] == "ASSESSED"
    assert settled["assessment_id"] == assessment_id


def test_trust_revision_stale_blocks_done_gates(api, objects):
    """第150批：trust_revision 漂移后 ASSESSED 结论失效，挡 DONE 门禁。"""
    import os

    import pytest
    from control_kernel.protocols.runtime import PlanRejected
    from control_kernel.storage.obligations import (
        assert_attempt_obligations_assessed,
        assert_no_pending_obligations,
    )
    from sqlalchemy import create_engine

    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    activity_id = UUID(lease["activity"]["id"])
    attempt_id = UUID(lease["lease"]["attempt_id"])
    assignment = lease["activity"]["verification_assignments"][0]
    receipt_id = receipt_artifact(engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
    )

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        # 漂移 trust_revision（模拟来源撤销推进）
        db.execute(
            text(
                """UPDATE project_trust_states
                SET trust_revision=trust_revision+1
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )
        with pytest.raises(PlanRejected, match="TRUST_REVISION_STALE"):
            assert_no_pending_obligations(db, subject_id=UUID(candidate["id"]))
        with pytest.raises(PlanRejected, match="TRUST_REVISION_STALE"):
            assert_attempt_obligations_assessed(
                db, activity_id=activity_id, attempt_id=attempt_id
            )
    eng.dispose()


def test_trust_blocked_rejects_verification_run(api, objects):
    """第150批：信任 BLOCKED 时禁止新 VerificationRun。"""
    from verification_run_helpers import link_settled_verification_effect, run_body

    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    assignment = lease["activity"]["verification_assignments"][0]
    receipt_id = receipt_artifact(engine, store, project_id)
    link_settled_verification_effect(engine, lease, input_artifact_id=receipt_id)

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states SET status='BLOCKED'
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )

    run = run_body(
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
    )
    run["verifier_digest"] = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run["receipt_ids"] = [str(receipt_id)]
    denied = client.post(
        "/internal/v1/verification-runs",
        json={"lease": lease["lease"], "run": run},
        headers=worker_auth,
    )
    # 恢复信任，避免污染后续用例
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states SET status='OPEN'
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )
    assert denied.status_code == 422, denied.text
    assert "TRUST_BLOCKED" in denied.json()["error"]["message"]


def _post_assessed_audit(api, objects):
    """封存候选 → claim AUDIT → VerificationRun → ASSESSED。"""
    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    activity_id = UUID(lease["activity"]["id"])
    attempt_id = UUID(lease["lease"]["attempt_id"])
    assignment = lease["activity"]["verification_assignments"][0]
    receipt_id = receipt_artifact(engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
    )
    with engine.connect() as db:
        assessment_id = db.execute(
            text(
                """SELECT assessment_id FROM verification_obligations
                WHERE activity_id=:activity AND attempt_id=:attempt"""
            ),
            {"activity": activity_id, "attempt": attempt_id},
        ).scalar_one()
        profile_id = UUID(assignment["verification_profile_id"])
    return {
        "engine": engine,
        "candidate": candidate,
        "activity_id": activity_id,
        "attempt_id": attempt_id,
        "assessment_id": assessment_id,
        "profile_id": profile_id,
        "project_id": project_id,
    }


def test_subject_digest_stale_blocks_done_gates(api, objects):
    """第151批：assessment.subject_digest 漂移后挡 DONE 门禁。"""
    import os

    import pytest
    from control_kernel.protocols.runtime import PlanRejected
    from control_kernel.storage.obligations import (
        assert_attempt_obligations_assessed,
        assert_no_pending_obligations,
    )
    from sqlalchemy import create_engine

    ctx = _post_assessed_audit(api, objects)
    stale = "sha256:" + "b" * 64
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        # 不可变表：测试事务内跳过 trigger，模拟评估后 subject 合同漂移
        db.execute(text("SET LOCAL session_replication_role = 'replica'"))
        db.execute(
            text(
                """UPDATE verification_assessments
                SET subject_digest=:d WHERE id=:id"""
            ),
            {"d": stale, "id": ctx["assessment_id"]},
        )
        db.execute(
            text(
                """UPDATE verification_runs
                SET subject_digest=:d
                WHERE id=(SELECT run_id FROM verification_assessments WHERE id=:id)"""
            ),
            {"d": stale, "id": ctx["assessment_id"]},
        )
        with pytest.raises(PlanRejected, match="SUBJECT_DIGEST_STALE"):
            assert_no_pending_obligations(db, subject_id=UUID(ctx["candidate"]["id"]))
        with pytest.raises(PlanRejected, match="SUBJECT_DIGEST_STALE"):
            assert_attempt_obligations_assessed(
                db,
                activity_id=ctx["activity_id"],
                attempt_id=ctx["attempt_id"],
            )
    eng.dispose()


def test_profile_digest_stale_blocks_done_gates(api, objects):
    """第151批：run.verifier_digest 相对 profile 漂移后挡 DONE 门禁。"""
    import os

    import pytest
    from control_kernel.protocols.runtime import PlanRejected
    from control_kernel.storage.obligations import (
        assert_attempt_obligations_assessed,
        assert_no_pending_obligations,
    )
    from sqlalchemy import create_engine

    ctx = _post_assessed_audit(api, objects)
    drifted = "sha256:" + "c" * 64
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        db.execute(text("SET LOCAL session_replication_role = 'replica'"))
        # profile.verifier_digest 为生成列不可手改；漂移 run 侧摘要模拟 profile 合同变更后旧 run
        db.execute(
            text(
                """UPDATE verification_runs
                SET verifier_digest=:d
                WHERE id=(SELECT run_id FROM verification_assessments WHERE id=:id)"""
            ),
            {"d": drifted, "id": ctx["assessment_id"]},
        )
        with pytest.raises(PlanRejected, match="PROFILE_DIGEST_STALE"):
            assert_no_pending_obligations(db, subject_id=UUID(ctx["candidate"]["id"]))
        with pytest.raises(PlanRejected, match="PROFILE_DIGEST_STALE"):
            assert_attempt_obligations_assessed(
                db,
                activity_id=ctx["activity_id"],
                attempt_id=ctx["attempt_id"],
            )
    eng.dispose()
