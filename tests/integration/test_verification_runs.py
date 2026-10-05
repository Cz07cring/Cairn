"""VerificationRun + Kernel Assessment：登记、幂等、冲突与 assignment 校验。"""

from uuid import UUID, uuid4

from control_kernel.domain.verification import EVALUATOR_DIGEST
from sqlalchemy import text
from test_audits import _seal_and_finish_execute
from test_claims import _register_worker
from verification_run_helpers import (
    link_settled_verification_effect,
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
    run_body,
    verification_run_digest,
)

# 兼容既有调用名
_digest = verification_run_digest
_receipt_artifact = receipt_artifact
_profile_verifier_digest = profile_verifier_digest
_run_body = run_body
_post_verification_run = post_verification_run


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
    assert lease["activity"]["target"]["id"] == candidate["id"]
    return worker_auth, lease


def _post_run(client, worker_auth, lease, run):
    return client.post(
        "/internal/v1/verification-runs",
        json={"lease": lease["lease"], "run": run},
        headers=worker_auth,
    )


def test_verification_run_happy_get_idempotent_conflict(api, objects):
    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    receipt_id = _receipt_artifact(engine, store, project_id)

    assignment = lease["activity"]["verification_assignments"][0]
    verifier_digest = _profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run = _run_body(
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
    )
    run["verifier_digest"] = verifier_digest
    run["receipt_ids"] = [str(receipt_id)]

    # 空义务禁止发出
    denied_empty = _post_run(client, worker_auth, lease, run)
    assert denied_empty.status_code == 422, denied_empty.text
    assert "未关联验证动作" in denied_empty.json()["error"]["message"]

    link_settled_verification_effect(engine, lease, input_artifact_id=receipt_id)

    created = _post_run(client, worker_auth, lease, run)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["content_digest"].startswith("sha256:")
    run_id = data["id"]

    with engine.connect() as db:
        assessment = (
            db.execute(
                text("SELECT * FROM verification_assessments WHERE run_id=:id"),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        trust = db.execute(
            text("SELECT trust_revision FROM project_trust_states WHERE project_id=:id"),
            {"id": project_id},
        ).scalar_one()
    assert assessment["verdict"] == "PASS"
    assert assessment["evaluator_digest"] == EVALUATOR_DIGEST
    assert assessment["trust_revision"] == trust

    fetched = client.get(f"/internal/v1/verification-runs/{run_id}", headers=auth)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["id"] == run_id

    # 无 membership / 空 project_ids → scope 不可见
    stranger = {"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])}
    denied = client.get(f"/internal/v1/verification-runs/{run_id}", headers=stranger)
    assert denied.status_code == 404

    replay = _post_run(client, worker_auth, lease, run)
    assert replay.status_code == 201, replay.text
    assert replay.json()["data"]["id"] == run_id
    assert replay.json()["data"]["content_digest"] == data["content_digest"]

    conflict_run = {**run, "input_digest": _digest("other-input")}
    conflict = _post_run(client, worker_auth, lease, conflict_run)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "VERIFICATION_RUN_CONFLICT"

    with engine.connect() as db:
        assert (
            db.execute(
                text("SELECT content_digest FROM verification_runs WHERE id=:id"),
                {"id": run_id},
            ).scalar_one()
            == data["content_digest"]
        )


def test_verification_run_assignment_mismatch(api, objects):
    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    receipt_id = _receipt_artifact(engine, store, project_id)
    assignment = lease["activity"]["verification_assignments"][0]
    verifier_digest = _profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run = _run_body(
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        audit_round=99,
    )
    run["verifier_digest"] = verifier_digest
    run["receipt_ids"] = [str(receipt_id)]
    bad = _post_run(client, worker_auth, lease, run)
    assert bad.status_code == 422, bad.text


def test_verification_run_fail_verdict_still_201(api, objects):
    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    receipt_id = _receipt_artifact(engine, store, project_id)
    assignment = lease["activity"]["verification_assignments"][0]
    verifier_digest = _profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run = _run_body(
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        checks_passed="false",
    )
    run["verifier_digest"] = verifier_digest
    run["receipt_ids"] = [str(receipt_id)]
    link_settled_verification_effect(engine, lease, input_artifact_id=receipt_id)
    created = _post_run(client, worker_auth, lease, run)
    assert created.status_code == 201, created.text
    with engine.connect() as db:
        verdict = db.execute(
            text("SELECT verdict FROM verification_assessments WHERE run_id=:id"),
            {"id": created.json()["data"]["id"]},
        ).scalar_one()
    assert verdict == "FAIL"


def test_verification_run_rejects_inflight_effect(api, objects):
    """第153批：在途 effect 禁止发出 VerificationRun，避免 OPEN+assessment 死锁。"""
    from verification_run_helpers import link_verification_effect

    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    worker_auth, lease = _claim_audit(client, token, candidate)
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    receipt_id = _receipt_artifact(engine, store, project_id)
    assignment = lease["activity"]["verification_assignments"][0]
    verifier_digest = _profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run = _run_body(
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
    )
    run["verifier_digest"] = verifier_digest
    run["receipt_ids"] = [str(receipt_id)]
    link_verification_effect(
        engine, lease, input_artifact_id=receipt_id, status="PREPARED"
    )
    denied = _post_run(client, worker_auth, lease, run)
    assert denied.status_code == 422, denied.text
    assert "验证动作未结算" in denied.json()["error"]["message"]
    with engine.connect() as db:
        n_runs = db.execute(
            text(
                """SELECT count(*) FROM verification_runs
                WHERE producer_activity_id=:a AND producer_attempt_id=:t"""
            ),
            {
                "a": UUID(lease["activity"]["id"]),
                "t": UUID(lease["lease"]["attempt_id"]),
            },
        ).scalar_one()
        n_assess = db.execute(
            text(
                """SELECT count(*) FROM verification_assessments a
                JOIN verification_runs r ON r.id = a.run_id
                WHERE r.producer_activity_id=:a AND r.producer_attempt_id=:t"""
            ),
            {
                "a": UUID(lease["activity"]["id"]),
                "t": UUID(lease["lease"]["attempt_id"]),
            },
        ).scalar_one()
        obl = db.execute(
            text(
                """SELECT status FROM verification_obligations
                WHERE activity_id=:a AND attempt_id=:t"""
            ),
            {
                "a": UUID(lease["activity"]["id"]),
                "t": UUID(lease["lease"]["attempt_id"]),
            },
        ).scalar_one()
    assert n_runs == 0
    assert n_assess == 0
    assert obl == "OPEN"
