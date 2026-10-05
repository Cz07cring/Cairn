"""EvidenceValidityDecision 登记、传播推进与 CAS 解锁（Issue #23 / doc/05 §3.11）。

无通用 unlock API：仅传播 job COMPLETE 且仍为当前 propagation_job 时，
才可将 TrustState 置回 OPEN。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..protocols.runtime import PlanRejected
from ..protocols.trust import TrustInvalidationCreate, TrustPropagationJobResource
from .policies import ConfigurationVersions, ScopeNotFound


class TrustRevisionConflict(Exception):
    """expected_trust_revision 与当前不一致；须重读后再提交。"""


def _job_from_row(row) -> TrustPropagationJobResource:
    return TrustPropagationJobResource(
        id=row["id"],
        project_id=row["project_id"],
        decision_ids=list(row["decision_ids"] or []),
        status=row["status"],
        cursor=row["cursor"],
        affected_skill_ids=list(row["affected_skill_ids"] or []),
        affected_goal_ids=list(row["affected_goal_ids"] or []),
        affected_release_ids=list(row["affected_release_ids"] or []),
        deadline_at=row["deadline_at"],
        reason_code=row["reason_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_trust_propagation_job(
    engine: Engine,
    job_id: UUID,
    subject: str,
    project_ids: list[str],
) -> TrustPropagationJobResource:
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM trust_propagation_jobs WHERE id=:id"),
                {"id": job_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
        return _job_from_row(row)


def submit_evidence_invalidation(
    engine: Engine,
    subject: str,
    project_ids: list[str],
    body: TrustInvalidationCreate,
) -> TrustPropagationJobResource:
    """登记 INVALID 决定并封锁项目信任；幂等重传返回原 job。"""
    expected = int(body.expected_trust_revision)
    with engine.begin() as db:
        ConfigurationVersions.check_scope(db, body.project_id, subject, project_ids)

        trust = (
            db.execute(
                text(
                    """SELECT trust_revision, status, decision_ids, propagation_job_id
                    FROM project_trust_states
                    WHERE project_id=:id FOR UPDATE"""
                ),
                {"id": body.project_id},
            )
            .mappings()
            .one()
        )

        existing_decision = (
            db.execute(
                text(
                    """SELECT id FROM evidence_validity_decisions
                    WHERE project_id=:p AND evidence_id=:e"""
                ),
                {"p": body.project_id, "e": body.evidence_id},
            )
            .mappings()
            .first()
        )
        if existing_decision is not None:
            job_id = trust["propagation_job_id"]
            if job_id is None:
                raise PlanRejected("信任失效决定已存在但缺少 propagation_job")
            job = (
                db.execute(
                    text("SELECT * FROM trust_propagation_jobs WHERE id=:id"),
                    {"id": job_id},
                )
                .mappings()
                .one()
            )
            return _job_from_row(job)

        if int(trust["trust_revision"]) != expected:
            raise TrustRevisionConflict()

        evidence = db.execute(
            text(
                """SELECT 1 FROM evidence_envelopes
                WHERE project_id=:p AND id=:id"""
            ),
            {"p": body.project_id, "id": body.evidence_id},
        ).first()
        if evidence is None:
            raise ScopeNotFound()

        for aid in body.proof_artifact_ids:
            found = db.execute(
                text(
                    """SELECT 1 FROM artifacts
                    WHERE project_id=:p AND id=:id"""
                ),
                {"p": body.project_id, "id": aid},
            ).first()
            if found is None:
                raise PlanRejected("proof_artifact_ids 含未登记工件")

        decision_id = uuid4()
        job_id = uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(hours=24)
        authority = f"ledger-ops:{subject}"

        db.execute(
            text(
                """INSERT INTO evidence_validity_decisions(
                  id,project_id,evidence_id,decision,reason_code,
                  authority_identity,proof_artifact_ids)
                VALUES(
                  :id,:project,:evidence,'INVALID',:reason,
                  :authority,:proofs)"""
            ),
            {
                "id": decision_id,
                "project": body.project_id,
                "evidence": body.evidence_id,
                "reason": body.reason_code,
                "authority": authority,
                "proofs": list(body.proof_artifact_ids),
            },
        )

        prior_decisions = list(trust["decision_ids"] or [])
        merged = prior_decisions + [decision_id]
        db.execute(
            text(
                """INSERT INTO trust_propagation_jobs(
                  id,project_id,decision_ids,status,deadline_at)
                VALUES(
                  :id,:project,:decisions,'PENDING',:deadline)"""
            ),
            {
                "id": job_id,
                "project": body.project_id,
                "decisions": merged,
                "deadline": deadline,
            },
        )
        db.execute(
            text(
                """UPDATE project_trust_states
                SET trust_revision=trust_revision+1,
                    status='BLOCKED',
                    decision_ids=:decisions,
                    propagation_job_id=:job
                WHERE project_id=:id"""
            ),
            {
                "id": body.project_id,
                "decisions": merged,
                "job": job_id,
            },
        )
        job = (
            db.execute(
                text("SELECT * FROM trust_propagation_jobs WHERE id=:id"),
                {"id": job_id},
            )
            .mappings()
            .one()
        )
        return _job_from_row(job)


def _scan_affected_for_decisions(db, *, project_id: UUID, decision_ids: list[UUID]):
    """有界扫描：失效 evidence → goal / ACTIVE skill / 已发布 Release。"""
    if not decision_ids:
        return [], [], []
    evidence_ids = (
        db.execute(
            text(
                """SELECT evidence_id FROM evidence_validity_decisions
                WHERE project_id=:p AND id = ANY(:ids)"""
            ),
            {"p": project_id, "ids": decision_ids},
        )
        .scalars()
        .all()
    )
    if not evidence_ids:
        return [], [], []
    evidence_list = list(evidence_ids)
    goal_ids = (
        db.execute(
            text(
                """SELECT DISTINCT goal_id FROM evidence_envelopes
                WHERE project_id=:p AND id = ANY(:ids) AND goal_id IS NOT NULL
                ORDER BY goal_id"""
            ),
            {"p": project_id, "ids": evidence_list},
        )
        .scalars()
        .all()
    )
    # Skill：校验记录命中失效信封 → 撤销 ACTIVE/CANDIDATE/VALIDATING（doc/05 §3.11）
    skill_ids = (
        db.execute(
            text(
                """SELECT DISTINCT v.subject_skill_version_id
                FROM skill_validation_records v
                JOIN skill_versions s
                  ON s.id = v.subject_skill_version_id AND s.project_id = v.project_id
                WHERE v.project_id=:p
                  AND s.status IN ('ACTIVE','CANDIDATE','VALIDATING')
                  AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(
                      COALESCE(v.content->'evidence_ids','[]'::jsonb)
                    ) AS e(eid)
                    WHERE e.eid::uuid = ANY(:ids)
                  )
                ORDER BY v.subject_skill_version_id"""
            ),
            {"p": project_id, "ids": evidence_list},
        )
        .scalars()
        .all()
    )
    # Release：清单显式列出失效 evidence，或同 Goal 信封命中 → 追加 INVALIDATED
    release_ids = (
        db.execute(
            text(
                """SELECT DISTINCT r.id
                FROM release_manifests r
                WHERE r.project_id=:p
                  AND (
                    EXISTS (
                      SELECT 1 FROM unnest(r.evidence_ids) AS eid
                      WHERE eid = ANY(:ids)
                    )
                    OR EXISTS (
                      SELECT 1 FROM evidence_envelopes e
                      WHERE e.project_id=:p
                        AND e.id = ANY(:ids)
                        AND e.goal_id = r.goal_id
                    )
                  )
                ORDER BY r.id"""
            ),
            {"p": project_id, "ids": evidence_list},
        )
        .scalars()
        .all()
    )
    return list(goal_ids), list(skill_ids), list(release_ids)


def _revoke_skills_for_invalid_evidence(
    db, *, project_id: UUID, skill_version_ids: list[UUID]
) -> list[UUID]:
    """将命中的 ACTIVE/CANDIDATE/VALIDATING Skill 置 REVOKED（VALIDATION_EVIDENCE_INVALID）。"""
    revoked: list[UUID] = []
    reason = "VALIDATION_EVIDENCE_INVALID"
    for version_id in skill_version_ids:
        skill_lock = json.dumps(
            ["skill", str(project_id), str(version_id)], separators=(",", ":")
        )
        # 与 Skills.revoke 同序：skill scope 锁
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": skill_lock},
        )
        updated = (
            db.execute(
                text(
                    """UPDATE skill_versions
                    SET status='REVOKED',
                        revocation_reason=:reason,
                        updated_at=clock_timestamp()
                    WHERE id=:id AND project_id=:p
                      AND status IN ('ACTIVE','CANDIDATE','VALIDATING')
                    RETURNING id,skill_id,created_at,updated_at,version,content_digest,
                              config,status,audit_id,revocation_reason"""
                ),
                {"id": version_id, "p": project_id, "reason": reason},
            )
            .mappings()
            .first()
        )
        if updated is None:
            continue
        revoked.append(updated["id"])
        db.execute(
            text(
                """INSERT INTO project_events(id,project_id,kind,payload)
                VALUES(:id,:project,'SKILL_VERSION_REVOKED',CAST(:payload AS jsonb))"""
            ),
            {
                "id": uuid4(),
                "project": project_id,
                "payload": json.dumps(
                    {
                        "id": str(updated["id"]),
                        "skill_id": str(updated["skill_id"]),
                        "status": updated["status"],
                        "revocation_reason": updated["revocation_reason"],
                    },
                    separators=(",", ":"),
                ),
            },
        )
    return revoked

def _invalidate_releases_for_decisions(
    db,
    *,
    project_id: UUID,
    release_ids: list[UUID],
    decision_ids: list[UUID],
) -> list[UUID]:
    """对已发布清单追加 ReleaseValidityRecord(INVALIDATED)；不改历史 Manifest、不复活 Goal。"""
    if not release_ids or not decision_ids:
        return []
    # 数组相等幂等：排序后比较，避免同集不同序重复插入
    decisions = sorted(decision_ids, key=str)
    invalidated: list[UUID] = []
    for release_id in release_ids:
        existing = db.execute(
            text(
                """SELECT 1 FROM release_validity_records
                WHERE project_id=:p
                  AND release_manifest_id=:r
                  AND decision_ids = :d
                LIMIT 1"""
            ),
            {"p": project_id, "r": release_id, "d": decisions},
        ).first()
        if existing is not None:
            invalidated.append(release_id)
            continue
        db.execute(
            text(
                """INSERT INTO release_validity_records(
                  id,project_id,release_manifest_id,status,decision_ids)
                VALUES(:id,:project,:release,'INVALIDATED',:decisions)"""
            ),
            {
                "id": uuid4(),
                "project": project_id,
                "release": release_id,
                "decisions": decisions,
            },
        )
        invalidated.append(release_id)
    return invalidated


_TERMINAL_GOAL = frozenset({"DONE", "FAILED", "CANCELLED"})
_ENGINEERING_KINDS = ("PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE")
# 稳定命名空间：同 job+attempt 重 advance 不重复插 Stop
_TRUST_DRAIN_NS = UUID("a7e0c3d1-5b2f-4e89-9c1a-0d4f6b8e2a10")


def _drain_affected_goal_writers(
    db, *, project_id: UUID, goal_ids: list[UUID], job_id: UUID
) -> None:
    """受影响非终态 Goal：取消 READY 工程活动，对 RUNNING 发 TRUST_INVALIDATION Stop（不盲取消）。

    保留预算/checkpoint；不写 Goal DONE/FAILED。RECONCILE 准入不由此取消。
    """
    from datetime import UTC, datetime, timedelta

    from ..protocols.runtime import StopRequest
    from .stops import insert_stop_request

    for goal_id in goal_ids:
        goal = (
            db.execute(
                text(
                    """SELECT id, status FROM goals
                    WHERE id=:id AND project_id=:p FOR UPDATE"""
                ),
                {"id": goal_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if goal is None or goal["status"] in _TERMINAL_GOAL:
            continue
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE goal_id=:goal AND status='READY' AND kind = ANY(:kinds)"""
            ),
            {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
        )
        rows = (
            db.execute(
                text(
                    """SELECT a.id AS activity_id, a.project_id,
                              att.id AS attempt_id, att.fencing_epoch
                    FROM activities a
                    JOIN activity_attempts att ON att.id=a.current_attempt_id
                    WHERE a.goal_id=:goal
                      AND a.status='RUNNING'
                      AND a.kind = ANY(:kinds)
                      AND att.status='ACTIVE'"""
                ),
                {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
            )
            .mappings()
            .all()
        )
        deadline = datetime.now(UTC) + timedelta(hours=1)
        for row in rows:
            attempt_id = row["attempt_id"]
            # 幂等：同一传播 job 对同一 attempt 只登记一次 Stop
            request_id = uuid5(_TRUST_DRAIN_NS, f"{job_id}:{attempt_id}")
            insert_stop_request(
                db,
                StopRequest(
                    request_id=request_id,
                    activation_id=attempt_id,
                    activity_id=row["activity_id"],
                    attempt_id=attempt_id,
                    fencing_epoch=str(row["fencing_epoch"]),
                    reason="TRUST_INVALIDATION",
                    deadline_at=deadline,
                ),
                row["project_id"],
            )


def _affected_goals_still_draining(db, *, goal_ids: list[UUID]) -> bool:
    """仍有未结算写入或未确认 TRUST_INVALIDATION Stop 时，不得 CAS 解锁项目信任。

    RUNNING 行本身不是权威：Stop CONFIRMED 才表示计算/写权限已排空；
    不以假取消 Activity 冒充排空。
    """
    if not goal_ids:
        return False
    unsettled = db.execute(
        text(
            """SELECT 1 FROM effect_intents e
            JOIN activities a ON a.id=e.activity_id
            WHERE a.goal_id = ANY(:goals)
              AND e.status IN ('DISPATCHED','UNKNOWN')
            LIMIT 1"""
        ),
        {"goals": goal_ids},
    ).first()
    if unsettled is not None:
        return True
    pending_stop = db.execute(
        text(
            """SELECT 1 FROM stops s
            JOIN activities a ON a.id=s.activity_id
            WHERE a.goal_id = ANY(:goals)
              AND s.reason='TRUST_INVALIDATION'
              AND s.status IN ('REQUESTED','UNCONFIRMED')
            LIMIT 1"""
        ),
        {"goals": goal_ids},
    ).first()
    return pending_stop is not None


def _try_cas_unlock(db, *, project_id: UUID, job_id: UUID) -> bool:
    """仅当本 job 仍是当前传播、无其他未决 job、且受影响 Goal 已排空写入时置 OPEN。"""
    trust = (
        db.execute(
            text(
                """SELECT status, propagation_job_id FROM project_trust_states
                WHERE project_id=:id FOR UPDATE"""
            ),
            {"id": project_id},
        )
        .mappings()
        .one()
    )
    if trust["status"] != "BLOCKED":
        return False
    if trust["propagation_job_id"] != job_id:
        return False
    job = (
        db.execute(
            text(
                """SELECT affected_goal_ids FROM trust_propagation_jobs
                WHERE id=:id"""
            ),
            {"id": job_id},
        )
        .mappings()
        .one()
    )
    if _affected_goals_still_draining(
        db, goal_ids=list(job["affected_goal_ids"] or [])
    ):
        return False
    pending = db.execute(
        text(
            """SELECT 1 FROM trust_propagation_jobs
            WHERE project_id=:p AND status IN ('PENDING','RUNNING')
            LIMIT 1"""
        ),
        {"p": project_id},
    ).first()
    if pending is not None:
        return False
    updated = db.execute(
        text(
            """UPDATE project_trust_states
            SET status='OPEN', propagation_job_id=NULL
            WHERE project_id=:id
              AND status='BLOCKED'
              AND propagation_job_id=:job
            RETURNING project_id"""
        ),
        {"id": project_id, "job": job_id},
    ).first()
    return updated is not None


def advance_trust_propagation(
    engine: Engine,
    job_id: UUID,
    subject: str,
    project_ids: list[str],
) -> TrustPropagationJobResource:
    """推进传播：扫描受影响 goal → COMPLETE；满足条件则 CAS 解锁 OPEN。

    幂等：已 COMPLETE 时仍尝试解锁（若仍被本 job 封锁）。
    """
    with engine.begin() as db:
        job = (
            db.execute(
                text(
                    """SELECT * FROM trust_propagation_jobs WHERE id=:id FOR UPDATE"""
                ),
                {"id": job_id},
            )
            .mappings()
            .first()
        )
        if job is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, job["project_id"], subject, project_ids)

        db.execute(
            text(
                """SELECT 1 FROM project_trust_states
                WHERE project_id=:id FOR UPDATE"""
            ),
            {"id": job["project_id"]},
        )

        if job["status"] in ("PENDING", "RUNNING"):
            goals, skills, releases = _scan_affected_for_decisions(
                db,
                project_id=job["project_id"],
                decision_ids=list(job["decision_ids"] or []),
            )
            revoked = _revoke_skills_for_invalid_evidence(
                db, project_id=job["project_id"], skill_version_ids=skills
            )
            # 仅记录实际撤销成功的 skill，避免误报未 ACTIVE 项
            skills = revoked
            releases = _invalidate_releases_for_decisions(
                db,
                project_id=job["project_id"],
                release_ids=releases,
                decision_ids=list(job["decision_ids"] or []),
            )
            _drain_affected_goal_writers(
                db,
                project_id=job["project_id"],
                goal_ids=goals,
                job_id=job_id,
            )
            job = (
                db.execute(
                    text(
                        """UPDATE trust_propagation_jobs
                        SET status='COMPLETE',
                            cursor='scan:goals+skills+releases+drain:done',
                            affected_goal_ids=:goals,
                            affected_skill_ids=:skills,
                            affected_release_ids=:releases,
                            updated_at=clock_timestamp()
                        WHERE id=:id AND status IN ('PENDING','RUNNING')
                        RETURNING *"""
                    ),
                    {
                        "id": job_id,
                        "goals": goals,
                        "skills": skills,
                        "releases": releases,
                    },
                )
                .mappings()
                .one()
            )
        elif job["status"] == "BLOCKED":
            raise PlanRejected("传播任务已阻塞，不能推进")

        _try_cas_unlock(db, project_id=job["project_id"], job_id=job_id)
        row = (
            db.execute(
                text("SELECT * FROM trust_propagation_jobs WHERE id=:id"),
                {"id": job_id},
            )
            .mappings()
            .one()
        )
        return _job_from_row(row)
