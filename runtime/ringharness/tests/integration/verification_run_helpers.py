"""集成测共用：登记与 lease 对齐的 VerificationRun。"""

import hashlib
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.obligations import link_effect
from sqlalchemy import text


def verification_run_digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def receipt_artifact(engine, store, project_id: UUID) -> UUID:
    blob = b"verification-receipt"
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/octet-stream",
        producer_identity="test:verification-receipt",
    )
    return art.id


def profile_verifier_digest(client, auth, project_id, profile_id) -> str:
    rows = client.get(
        "/api/v1/verification-profiles",
        params={"project_id": project_id},
        headers=auth,
    )
    assert rows.status_code == 200, rows.text
    for row in rows.json()["data"]:
        if row["id"] == profile_id:
            return row["config"]["verifier_digest"]
    raise AssertionError("profile missing")


def run_body(
    lease,
    *,
    subject_id,
    subject_digest,
    subject_type: str = "CANDIDATE",
    checks_passed: str = "true",
    criterion_id: str = "A1",
    audit_round=None,
):
    assignment = lease["activity"]["verification_assignments"][0]
    return {
        "project_id": lease["activity"]["project_id"],
        "producer_activity_id": lease["activity"]["id"],
        "producer_attempt_id": lease["lease"]["attempt_id"],
        "subject_type": subject_type,
        "subject_id": subject_id,
        "subject_digest": subject_digest,
        "verification_profile_id": assignment["verification_profile_id"],
        "verifier_digest": None,
        "audit_round": audit_round if audit_round is not None else assignment["audit_round"],
        "layer": assignment["layer"],
        "input_digest": verification_run_digest("input"),
        "environment_digest": verification_run_digest("env"),
        "receipt_ids": [],
        "observations": [
            {
                "criterion_id": criterion_id,
                "metric": "checks_passed",
                "value": checks_passed,
                "status": "OBSERVED",
                "evidence_ids": [str(uuid4())],
                "reason_code": "MEASURED",
            }
        ],
    }


def link_verification_effect(
    engine, lease, *, input_artifact_id: UUID, status: str = "SUCCEEDED"
) -> UUID:
    """为当前 OPEN 义务挂一条指定状态的 effect（默认已结算）。"""
    activity = lease["activity"]
    attempt_id = UUID(lease["lease"]["attempt_id"])
    activity_id = UUID(activity["id"])
    project_id = UUID(activity["project_id"])
    goal_id = UUID(activity["goal_id"]) if activity.get("goal_id") else None
    effect_id = uuid4()
    step_id = uuid4()
    with engine.begin() as db:
        obl = (
            db.execute(
                text(
                    """SELECT id, effect_ids, invocation_ids FROM verification_obligations
                    WHERE activity_id=:activity AND attempt_id=:attempt
                      AND status='OPEN'
                    FOR UPDATE"""
                ),
                {"activity": activity_id, "attempt": attempt_id},
            )
            .mappings()
            .first()
        )
        if obl is None:
            raise AssertionError("缺少 OPEN VerificationObligation")
        if list(obl["effect_ids"] or []) or list(obl["invocation_ids"] or []):
            existing = list(obl["effect_ids"] or []) or list(obl["invocation_ids"] or [])
            return existing[0]
        db.execute(
            text(
                """INSERT INTO effect_intents(
                  id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                  payload_digest,tool_ref,replay_class,scope,status,
                  input_artifact_id,producer_attempt_id)
                VALUES(
                  :id,:project,:goal,:activity,:step,1,
                  :digest,'read_file','READ_ONLY','VERIFICATION',:status,
                  :artifact,:attempt)"""
            ),
            {
                "id": effect_id,
                "project": project_id,
                "goal": goal_id,
                "activity": activity_id,
                "step": step_id,
                "digest": "sha256:" + "d" * 64,
                "status": status,
                "artifact": input_artifact_id,
                "attempt": attempt_id,
            },
        )
        link_effect(db, obl["id"], effect_id)
    return effect_id


def link_settled_verification_effect(engine, lease, *, input_artifact_id: UUID) -> UUID:
    """为当前 OPEN 义务挂一条已结算 effect，满足「发出前须关联动作」。"""
    return link_verification_effect(
        engine, lease, input_artifact_id=input_artifact_id, status="SUCCEEDED"
    )


def post_verification_run(
    client,
    worker_auth,
    lease,
    *,
    subject_id,
    subject_digest,
    verifier_digest: str,
    receipt_id: UUID | str,
    subject_type: str = "CANDIDATE",
    criterion_id: str = "A1",
    checks_passed: str = "true",
    engine=None,
    link_action: bool = True,
    audit_round=None,
    effect_status: str = "SUCCEEDED",
    evidence_ids: list[str] | None = None,
    observations: list[dict] | None = None,
) -> str:
    """登记与当前 lease/assignment 对齐的 VerificationRun，返回 run_id。"""
    if link_action:
        eng = engine
        if eng is None:
            state = getattr(getattr(client, "app", None), "state", None)
            eng = getattr(state, "engine", None) if state is not None else None
        if eng is None:
            raise AssertionError("link_action 需要 engine（或 client.app.state.engine）")
        link_verification_effect(
            eng,
            lease,
            input_artifact_id=UUID(str(receipt_id)),
            status=effect_status,
        )
    run = run_body(
        lease,
        subject_id=subject_id,
        subject_digest=subject_digest,
        subject_type=subject_type,
        criterion_id=criterion_id,
        checks_passed=checks_passed,
        audit_round=audit_round,
    )
    run["verifier_digest"] = verifier_digest
    run["receipt_ids"] = [str(receipt_id)]
    if observations is not None:
        run["observations"] = observations
    elif evidence_ids is not None:
        run["observations"][0]["evidence_ids"] = [str(i) for i in evidence_ids]
    created = client.post(
        "/internal/v1/verification-runs",
        json={"lease": lease["lease"], "run": run},
        headers=worker_auth,
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


def ingest_pytest_evidence(
    engine,
    store,
    project_id: UUID,
    completed,
    *,
    producer_identity: str = "test:pytest-evidence",
) -> tuple[UUID, str]:
    """把真实 pytest stdout/stderr 入库，返回 (artifact_id, checks_passed)。"""
    import json

    payload = {
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[-8000:],
        "stderr": (completed.stderr or "")[-4000:],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(raw),
        mime="application/json",
        producer_identity=producer_identity,
    )
    checks = "true" if completed.returncode == 0 else "false"
    return art.id, checks


def post_verification_run_from_pytest(
    client,
    worker_auth,
    lease,
    *,
    subject_id,
    subject_digest,
    verifier_digest: str,
    completed,
    engine,
    store,
    project_id: UUID,
    subject_type: str = "CANDIDATE",
    criterion_id: str = "A1",
    audit_round=None,
    producer_identity: str = "test:pytest-evidence",
    link_action: bool = True,
) -> tuple[str, str]:
    """用真实 pytest CompletedProcess 生成 observation，返回 (run_id, checks_passed)。

    link_action=False：义务已由 AUDIT prepare 的 VERIFICATION effect 关联（Broker 跑测路径）。
    """
    evidence_id, checks = ingest_pytest_evidence(
        engine,
        store,
        project_id,
        completed,
        producer_identity=producer_identity,
    )
    run_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=subject_id,
        subject_digest=subject_digest,
        verifier_digest=verifier_digest,
        receipt_id=evidence_id,
        subject_type=subject_type,
        criterion_id=criterion_id,
        checks_passed=checks,
        engine=engine,
        audit_round=audit_round,
        evidence_ids=[str(evidence_id)],
        link_action=link_action,
    )
    return run_id, checks


def post_verification_run_from_broker_tests(
    client,
    worker_auth,
    lease,
    *,
    subject_id,
    subject_digest,
    verifier_digest: str,
    broker_ran: dict,
    engine,
    store,
    project_id: UUID,
    subject_type: str = "CANDIDATE",
    criterion_id: str = "A1",
    audit_round=None,
) -> tuple[str, str]:
    """用 Broker run_tests 回执构建 VerificationRun（义务已由 prepare 关联）。"""
    result = broker_ran["result"]
    checks = "true" if int(result.exit_code) == 0 else "false"
    # 复用 Broker 证据工件；stdout/stderr 摘要已在 content 内
    art_ids = list(broker_ran.get("result_artifact_ids") or [])
    if not art_ids:
        raise AssertionError("Broker run_tests 缺少 result_artifact_ids")
    receipt_id = art_ids[0]
    run_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=subject_id,
        subject_digest=subject_digest,
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        subject_type=subject_type,
        criterion_id=criterion_id,
        checks_passed=checks,
        engine=engine,
        audit_round=audit_round,
        evidence_ids=[str(i) for i in art_ids],
        link_action=False,
    )
    return run_id, checks
