"""VerificationRun 登记与同事务 Kernel Assessment。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..domain.verification import EVALUATOR_DIGEST, evaluate_metrics
from ..protocols.runtime import LeaseRejected, PlanRejected
from ..protocols.verification import (
    VerificationRunContent,
    VerificationRunCreateRequest,
    VerificationRunResource,
)
from .effects import _require_owner_attempt
from .policies import ConfigurationVersions, ScopeNotFound
from .probe import _content_digest


class VerificationRunConflict(Exception):
    """同提交键异 content_digest。"""


def _run_from_row(row) -> VerificationRunResource:
    return VerificationRunResource.model_validate(
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "project_id": row["project_id"],
            "producer_activity_id": row["producer_activity_id"],
            "producer_attempt_id": row["producer_attempt_id"],
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "subject_digest": row["subject_digest"],
            "verification_profile_id": row["verification_profile_id"],
            "verifier_digest": row["verifier_digest"],
            "audit_round": row["audit_round"],
            "layer": row["layer"],
            "input_digest": row["input_digest"],
            "environment_digest": row["environment_digest"],
            "receipt_ids": list(row["receipt_ids"] or []),
            "observations": row["observations"],
            "content_digest": row["content_digest"],
        }
    )


def _run_content_payload(run: VerificationRunContent) -> dict:
    return {
        "project_id": str(run.project_id),
        "producer_activity_id": str(run.producer_activity_id),
        "producer_attempt_id": str(run.producer_attempt_id),
        "subject_type": run.subject_type,
        "subject_id": str(run.subject_id),
        "subject_digest": run.subject_digest,
        "verification_profile_id": str(run.verification_profile_id),
        "verifier_digest": run.verifier_digest,
        "audit_round": run.audit_round,
        "layer": run.layer,
        "input_digest": run.input_digest,
        "environment_digest": run.environment_digest,
        "receipt_ids": [str(i) for i in run.receipt_ids],
        "observations": [o.model_dump(mode="json") for o in run.observations],
    }


def _load_criteria(db, activity, profile_id: UUID) -> list[dict]:
    if activity["kind"] == "AUDIT":
        task = (
            db.execute(
                text("SELECT contract FROM tasks WHERE id=:id"),
                {"id": activity["task_id"]},
            )
            .mappings()
            .first()
        )
        if task is None:
            raise PlanRejected("AUDIT 活动缺少 Task")
        contract = task["contract"]
        if isinstance(contract, str):
            contract = json.loads(contract)
        criteria = [
            c
            for c in (contract.get("acceptance") or [])
            if str(c.get("verification_profile_id")) == str(profile_id)
        ]
    elif activity["kind"] == "FINALIZE":
        # GLOBAL 终验准则来自 Goal 合同 success_criteria（与 assignment.criterion_id 对齐），非 Task acceptance。
        goal = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id"),
                {"id": activity["goal_id"]},
            )
            .mappings()
            .first()
        )
        if goal is None:
            raise PlanRejected("FINALIZE 活动缺少 Goal")
        contract = goal["contract"]
        if isinstance(contract, str):
            contract = json.loads(contract)
        criteria = [
            c
            for c in (contract.get("success_criteria") or [])
            if str(c.get("verification_profile_id")) == str(profile_id)
        ]
    elif activity["kind"] == "VALIDATE_SKILL":
        skill = (
            db.execute(
                text("SELECT config FROM skill_versions WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .first()
        )
        if skill is None:
            raise PlanRejected("VALIDATE_SKILL 缺少 SkillVersion")
        config = skill["config"]
        if isinstance(config, str):
            config = json.loads(config)
        criteria = [
            c
            for c in (config.get("acceptance") or [])
            if str(c.get("verification_profile_id")) == str(profile_id)
        ]
    else:
        raise PlanRejected("仅 AUDIT/FINALIZE/VALIDATE_SKILL 可登记 VerificationRun")
    if not criteria:
        raise PlanRejected("profile 无对应验收准则")
    return criteria


def _subject_digest(db, activity) -> str:
    # AUDIT/FINALIZE 的 target 均为已封存候选（target_id = candidate_manifest_id）。
    if activity["kind"] in ("AUDIT", "FINALIZE"):
        row = (
            db.execute(
                text("SELECT content_digest FROM candidate_manifests WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PlanRejected("候选未封存")
        return row["content_digest"]
    row = (
        db.execute(
            text("SELECT content_digest FROM skill_versions WHERE id=:id"),
            {"id": activity["target_id"]},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PlanRejected("技能版本不存在")
    return row["content_digest"]


def create_run(
    engine: Engine, subject: str, body: VerificationRunCreateRequest
) -> VerificationRunResource:
    run = body.run
    lease = body.lease
    with engine.begin() as db:
        activity, attempt = _require_owner_attempt(db, subject, lease.activity_id, lease)
        now = datetime.now(UTC)
        expires = attempt["lease_expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能登记新 VerificationRun")
        if activity["kind"] not in ("AUDIT", "FINALIZE", "VALIDATE_SKILL"):
            raise PlanRejected("仅 AUDIT/FINALIZE/VALIDATE_SKILL 可登记 VerificationRun")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if run.producer_activity_id != lease.activity_id:
            raise PlanRejected("producer_activity_id 与租约不一致")
        if run.producer_attempt_id != attempt["id"]:
            raise PlanRejected("producer_attempt_id 与租约不一致")
        if run.project_id != activity["project_id"]:
            raise PlanRejected("project_id 与活动不一致")

        expected_subject = (
            "CANDIDATE"
            if activity["kind"] in ("AUDIT", "FINALIZE")
            else "SKILL_VERSION"
        )
        if run.subject_type != expected_subject:
            raise PlanRejected("subject_type 与活动 kind 不一致")
        if run.subject_id != activity["target_id"]:
            raise PlanRejected("subject_id 与活动 target 不一致")
        sealed = _subject_digest(db, activity)
        if run.subject_digest != sealed:
            raise PlanRejected("subject_digest 与已封存对象不一致")

        assignments = activity["verification_assignments"] or []
        if not assignments:
            raise PlanRejected("活动缺少 verification_assignments")
        assignment = assignments[0]
        if str(run.verification_profile_id) != str(assignment["verification_profile_id"]):
            raise PlanRejected("verification_profile 与分配不一致")
        if run.layer != assignment.get("layer"):
            raise PlanRejected("layer 与分配不一致")
        if int(run.audit_round) != int(assignment.get("audit_round", 0)):
            raise PlanRejected("audit_round 与分配不一致")

        profile = (
            db.execute(
                text(
                    """SELECT id, config, content_digest, verifier_ref, verifier_digest
                    FROM verification_profiles
                    WHERE id=:id AND project_id=:project"""
                ),
                {"id": run.verification_profile_id, "project": activity["project_id"]},
            )
            .mappings()
            .first()
        )
        if profile is None:
            raise PlanRejected("verification_profile 不存在")
        if run.verifier_digest != profile["verifier_digest"]:
            raise PlanRejected("verifier_digest 与 profile 绑定不一致")
        definition = (
            db.execute(
                text(
                    """SELECT content FROM verifier_definitions
                    WHERE project_id=:project AND ref=:ref AND content_digest=:digest"""
                ),
                {
                    "project": activity["project_id"],
                    "ref": profile["verifier_ref"],
                    "digest": profile["verifier_digest"],
                },
            )
            .mappings()
            .first()
        )
        if definition is None:
            raise PlanRejected("verifier_definition 未批准或不存在")

        # receipt_ids：本切片按同项目 artifacts 存在性核对（验证宿主常将回执结果工件记入此列表）。
        # effect_receipts 为 (effect_id, receipt_id) 复合主键，单 id 无法可靠 FK；完整回执义务链延后。
        for receipt_id in run.receipt_ids:
            found = db.execute(
                text(
                    """SELECT 1 FROM artifacts
                    WHERE project_id=:project AND id=:id"""
                ),
                {"project": activity["project_id"], "id": receipt_id},
            ).first()
            if found is None:
                raise PlanRejected("receipt_ids 含未登记工件")

        criteria = _load_criteria(db, activity, run.verification_profile_id)
        observations = [o.model_dump(mode="json") for o in run.observations]
        try:
            evaluation = evaluate_metrics(
                definition["content"],
                profile["config"],
                criteria,
                observations,
            )
        except ValueError as exc:
            raise PlanRejected(f"观察无法判定：{exc}") from exc

        content = _run_content_payload(run)
        digest = _content_digest("VerificationRun", content)

        existing = (
            db.execute(
                text(
                    """SELECT * FROM verification_runs
                    WHERE producer_activity_id=:activity
                      AND producer_attempt_id=:attempt
                      AND audit_round=:round
                      AND verification_profile_id=:profile
                      AND layer=:layer
                    FOR UPDATE"""
                ),
                {
                    "activity": run.producer_activity_id,
                    "attempt": run.producer_attempt_id,
                    "round": run.audit_round,
                    "profile": run.verification_profile_id,
                    "layer": run.layer,
                },
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["content_digest"] == digest:
                return _run_from_row(existing)
            raise VerificationRunConflict()

        from .obligations import assert_obligation_actions_linked_for_run

        # 仅新建路径：空义务不得冒充验证闭环（幂等/冲突已在上分支返回）
        assert_obligation_actions_linked_for_run(
            db,
            activity_id=run.producer_activity_id,
            attempt_id=run.producer_attempt_id,
            subject_id=run.subject_id,
            profile_id=run.verification_profile_id,
            layer=run.layer,
            audit_round=run.audit_round,
        )

        run_id = uuid4()
        trust_row = (
            db.execute(
                text(
                    """SELECT status, trust_revision FROM project_trust_states
                    WHERE project_id=:id FOR SHARE"""
                ),
                {"id": activity["project_id"]},
            )
            .mappings()
            .one()
        )
        if trust_row["status"] != "OPEN":
            raise PlanRejected("TRUST_BLOCKED: 项目信任已封锁，禁止登记 VerificationRun")
        trust_revision = trust_row["trust_revision"]

        criterion_results = []
        for criterion_id, verdict in sorted(evaluation["criterion_results"].items()):
            evidence: list[UUID] = []
            seen: set[UUID] = set()
            for obs in run.observations:
                if obs.criterion_id == criterion_id and obs.status == "OBSERVED":
                    for eid in obs.evidence_ids:
                        if eid not in seen:
                            seen.add(eid)
                            evidence.append(eid)
            criterion_results.append(
                {
                    "criterion_id": criterion_id,
                    "verdict": verdict,
                    "evidence_ids": [str(i) for i in evidence],
                    "reason": "METRIC_EVALUATED",
                }
            )

        assessment_content = {
            "run_id": str(run_id),
            "project_id": str(activity["project_id"]),
            "subject_type": run.subject_type,
            "subject_id": str(run.subject_id),
            "subject_digest": run.subject_digest,
            "verification_profile_id": str(run.verification_profile_id),
            "layer": run.layer,
            "audit_round": run.audit_round,
            "trust_revision": str(trust_revision),
            "evaluator_digest": EVALUATOR_DIGEST,
            "criterion_results": criterion_results,
            "verdict": evaluation["verdict"],
        }
        assessment_digest = _content_digest("VerificationAssessment", assessment_content)

        db.execute(
            text(
                """INSERT INTO verification_runs(
                  id,project_id,producer_activity_id,producer_attempt_id,
                  subject_type,subject_id,subject_digest,verification_profile_id,
                  verifier_digest,audit_round,layer,input_digest,environment_digest,
                  receipt_ids,observations,content_digest)
                VALUES(
                  :id,:project,:activity,:attempt,
                  :subject_type,:subject_id,:subject_digest,:profile,
                  :verifier,:round,:layer,:input,:env,
                  :receipts,CAST(:observations AS jsonb),:digest)"""
            ),
            {
                "id": run_id,
                "project": activity["project_id"],
                "activity": run.producer_activity_id,
                "attempt": run.producer_attempt_id,
                "subject_type": run.subject_type,
                "subject_id": run.subject_id,
                "subject_digest": run.subject_digest,
                "profile": run.verification_profile_id,
                "verifier": run.verifier_digest,
                "round": run.audit_round,
                "layer": run.layer,
                "input": run.input_digest,
                "env": run.environment_digest,
                "receipts": run.receipt_ids,
                "observations": json.dumps(observations),
                "digest": digest,
            },
        )
        assessment_id = uuid4()
        db.execute(
            text(
                """INSERT INTO verification_assessments(
                  id,run_id,project_id,subject_type,subject_id,subject_digest,
                  verification_profile_id,layer,audit_round,trust_revision,
                  evaluator_digest,criterion_results,verdict,content_digest)
                VALUES(
                  :id,:run,:project,:subject_type,:subject_id,:subject_digest,
                  :profile,:layer,:round,:trust,
                  :evaluator,CAST(:results AS jsonb),:verdict,:digest)"""
            ),
            {
                "id": assessment_id,
                "run": run_id,
                "project": activity["project_id"],
                "subject_type": run.subject_type,
                "subject_id": run.subject_id,
                "subject_digest": run.subject_digest,
                "profile": run.verification_profile_id,
                "layer": run.layer,
                "round": run.audit_round,
                "trust": trust_revision,
                "evaluator": EVALUATOR_DIGEST,
                "results": json.dumps(criterion_results),
                "verdict": evaluation["verdict"],
                "digest": assessment_digest,
            },
        )
        from .obligations import settle_obligation_after_assessment

        settle_obligation_after_assessment(
            db,
            activity_id=run.producer_activity_id,
            attempt_id=run.producer_attempt_id,
            subject_id=run.subject_id,
            profile_id=run.verification_profile_id,
            layer=run.layer,
            audit_round=run.audit_round,
            assessment_id=assessment_id,
        )
        row = (
            db.execute(
                text("SELECT * FROM verification_runs WHERE id=:id"),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        return _run_from_row(row)


def get_run(
    engine: Engine, run_id: UUID, subject: str, project_ids: list[str]
) -> VerificationRunResource:
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM verification_runs WHERE id=:id"),
                {"id": run_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
        return _run_from_row(row)


def require_matching_verifier_runs(
    db,
    *,
    run_ids: list[UUID],
    producer_activity_id: UUID,
    producer_attempt_id: UUID,
    subject_type: str,
    subject_id: UUID,
    verification_profile_id: UUID,
    layer: str,
    audit_round: int,
) -> None:
    """AUDIT/FINALIZE/VALIDATE_SKILL outcome：每个 verifier_run_id 必须存在且与 lease 分配对齐。"""
    if not run_ids:
        raise PlanRejected("缺少 VerificationRun")
    for run_id in run_ids:
        row = (
            db.execute(
                text("SELECT * FROM verification_runs WHERE id=:id"),
                {"id": run_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PlanRejected("VerificationRun 不存在")
        if row["producer_activity_id"] != producer_activity_id:
            raise PlanRejected("VerificationRun 与活动不一致")
        if row["producer_attempt_id"] != producer_attempt_id:
            raise PlanRejected("VerificationRun 与 attempt 不一致")
        if row["subject_type"] != subject_type:
            raise PlanRejected("VerificationRun subject_type 不一致")
        if row["subject_id"] != subject_id:
            raise PlanRejected("VerificationRun subject 不一致")
        if row["verification_profile_id"] != verification_profile_id:
            raise PlanRejected("VerificationRun profile 不一致")
        if row["layer"] != layer:
            raise PlanRejected("VerificationRun layer 不一致")
        if int(row["audit_round"]) != int(audit_round):
            raise PlanRejected("VerificationRun audit_round 不一致")
