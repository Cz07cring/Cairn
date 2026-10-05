"""PlanningFeedback：审计集合汇总后由 Kernel 写入，不可模型自写。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from ..protocols.feedback import PlanningFeedbackResource
from .probe import _content_digest


def _row_to_feedback(row) -> PlanningFeedbackResource:
    data = dict(row)
    return PlanningFeedbackResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "goal_id": data["goal_id"],
            "task_id": data["task_id"],
            "candidate_manifest_id": data["candidate_manifest_id"],
            "goal_contract_revision": data["goal_contract_revision"],
            "task_contract_revision": data["task_contract_revision"],
            "plan_revision": data["plan_revision"],
            "aggregation_digest": data["aggregation_digest"],
            "verdict": data["verdict"],
            "public_criterion_results": data["public_criterion_results"] or [],
            "blocking_reason_codes": list(data["blocking_reason_codes"] or []),
            "content_digest": data["content_digest"],
        }
    )


def maybe_create_planning_feedback(
    db: Connection,
    *,
    project_id: UUID,
    goal_id: UUID,
    task_id: UUID,
    candidate_id: UUID,
) -> PlanningFeedbackResource | None:
    """当该 Task 下 AUDIT 活动均已终态时，按审计集合写一条（幂等去重）。"""
    open_audit = db.execute(
        text(
            """SELECT 1 FROM activities
            WHERE task_id=:task AND kind='AUDIT'
              AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')
            LIMIT 1"""
        ),
        {"task": task_id},
    ).first()
    if open_audit is not None:
        return None

    audits = (
        db.execute(
            text(
                """SELECT * FROM audits
                WHERE task_id=:task AND subject_candidate_manifest_id=:candidate
                ORDER BY created_at, id"""
            ),
            {"task": task_id, "candidate": candidate_id},
        )
        .mappings()
        .all()
    )
    if not audits:
        return None

    goal = (
        db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": goal_id})
        .mappings()
        .one()
    )
    if goal["plan_revision"] is None:
        return None

    digests = sorted(a["content_digest"] for a in audits)
    aggregation_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(digests, separators=(",", ":")).encode()
        ).hexdigest()
    )
    verdicts = {a["verdict"] for a in audits}
    if verdicts == {"PASS"}:
        verdict = "PASS"
        blocking: list[str] = []
    elif "FAIL" in verdicts:
        verdict = "FAIL"
        blocking = ["AUDIT_FAIL"]
    else:
        verdict = "INSUFFICIENT"
        blocking = ["AUDIT_INSUFFICIENT"]

    # 公开逐标准：合并各层结果，同 criterion 取最差。
    rank = {"PASS": 0, "INSUFFICIENT": 1, "FAIL": 2}
    by_id: dict[str, dict] = {}
    for audit in audits:
        for item in audit["criterion_results"] or []:
            cid = item["criterion_id"]
            prev = by_id.get(cid)
            if prev is None or rank[item["verdict"]] > rank[prev["verdict"]]:
                by_id[cid] = {
                    "criterion_id": cid,
                    "verdict": item["verdict"],
                    "evidence_ids": item.get("evidence_ids") or [],
                    "reason": item.get("reason") or "",
                }
    public_results = sorted(by_id.values(), key=lambda x: x["criterion_id"])

    grev = audits[0]["goal_contract_revision"]
    trev = audits[0]["task_contract_revision"]
    plan_rev = int(goal["plan_revision"])

    existing = (
        db.execute(
            text(
                """SELECT * FROM planning_feedbacks
                WHERE goal_id=:goal
                  AND task_id IS NOT DISTINCT FROM :task
                  AND candidate_manifest_id=:candidate
                  AND goal_contract_revision=:grev
                  AND plan_revision=:prev
                  AND aggregation_digest=:adigest"""
            ),
            {
                "goal": goal_id,
                "task": task_id,
                "candidate": candidate_id,
                "grev": grev,
                "prev": plan_rev,
                "adigest": aggregation_digest,
            },
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return _row_to_feedback(existing)

    content = {
        "goal_id": str(goal_id),
        "task_id": str(task_id),
        "candidate_manifest_id": str(candidate_id),
        "goal_contract_revision": grev,
        "task_contract_revision": trev,
        "plan_revision": plan_rev,
        "aggregation_digest": aggregation_digest,
        "verdict": verdict,
        "public_criterion_results": public_results,
        "blocking_reason_codes": sorted(blocking),
    }
    digest = _content_digest("PlanningFeedback", content)
    feedback_id = uuid4()
    row = (
        db.execute(
            text(
                """INSERT INTO planning_feedbacks(
                  id,project_id,goal_id,task_id,candidate_manifest_id,
                  goal_contract_revision,task_contract_revision,plan_revision,
                  aggregation_digest,verdict,public_criterion_results,
                  blocking_reason_codes,content_digest)
                VALUES(
                  :id,:project,:goal,:task,:candidate,
                  :grev,:trev,:prev,
                  :adigest,:verdict,CAST(:results AS jsonb),
                  :blocking,:digest)
                RETURNING *"""
            ),
            {
                "id": feedback_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "candidate": candidate_id,
                "grev": grev,
                "trev": trev,
                "prev": plan_rev,
                "adigest": aggregation_digest,
                "verdict": verdict,
                "results": json.dumps(public_results),
                "blocking": blocking,
                "digest": digest,
            },
        )
        .mappings()
        .one()
    )
    return _row_to_feedback(row)


def list_feedback_for_goal(
    db: Connection, goal_id: UUID, *, limit: int = 100
) -> tuple[list[PlanningFeedbackResource], bool]:
    rows = (
        db.execute(
            text(
                """SELECT * FROM planning_feedbacks
                WHERE goal_id=:goal
                ORDER BY created_at DESC, id DESC
                LIMIT :limit"""
            ),
            {"goal": goal_id, "limit": limit + 1},
        )
        .mappings()
        .all()
    )
    truncated = len(rows) > limit
    return [_row_to_feedback(r) for r in rows[:limit]], truncated


def feedback_ids_for_planner(
    db: Connection,
    goal_id: UUID,
    *,
    goal_contract_revision: int | None,
    plan_revision: int | None,
    limit: int = 100,
) -> list[UUID]:
    if goal_contract_revision is None or plan_revision is None:
        return []
    rows = db.execute(
        text(
            """SELECT id FROM planning_feedbacks
            WHERE goal_id=:goal
              AND goal_contract_revision=:grev
              AND plan_revision=:prev
            ORDER BY created_at ASC, id ASC
            LIMIT :limit"""
        ),
        {
            "goal": goal_id,
            "grev": goal_contract_revision,
            "prev": plan_revision,
            "limit": limit,
        },
    ).scalars()
    return list(rows)


def record_feedback_consumptions(
    db: Connection,
    *,
    plan_activity_id: UUID,
    attempt_id: UUID,
    feedback_ids: list[UUID],
) -> None:
    for fid in feedback_ids:
        db.execute(
            text(
                """INSERT INTO planning_feedback_consumptions(
                  plan_activity_id,feedback_id,attempt_id)
                VALUES(:activity,:feedback,:attempt)
                ON CONFLICT DO NOTHING"""
            ),
            {
                "activity": plan_activity_id,
                "feedback": fid,
                "attempt": attempt_id,
            },
        )
