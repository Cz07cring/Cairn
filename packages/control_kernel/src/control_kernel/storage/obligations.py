"""VerificationObligation：claim 开立、动作关联、Assessment 后结算与 DONE 门禁。"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..protocols.runtime import PlanRejected
from ..protocols.verification import VerificationObligationResource
from .policies import ConfigurationVersions, ScopeNotFound

# effect_intents / model_invocations：可结算终态（不含 UNKNOWN——未对账不得 ASSESSED）
_EFFECT_SETTLED = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
_INVOCATION_SETTLED = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
_VERIFY_KINDS = frozenset({"AUDIT", "VALIDATE_SKILL", "FINALIZE"})
# 仅明确终态才调度 READY 后继；RECOVERING/READY 等失租恢复态不得误开并行 AUDIT
_VERIFY_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def _obligation_from_row(row) -> VerificationObligationResource:
    return VerificationObligationResource(
        id=row["id"],
        project_id=row["project_id"],
        activity_id=row["activity_id"],
        attempt_id=row["attempt_id"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        profile_id=row["profile_id"],
        layer=row["layer"],
        audit_round=int(row["audit_round"]),
        effect_ids=list(row["effect_ids"] or []),
        invocation_ids=list(row["invocation_ids"] or []),
        status=row["status"],
        assessment_id=row["assessment_id"],
        superseded_by_obligation_id=row["superseded_by_obligation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_obligations(
    engine: Engine,
    subject: str,
    project_ids: list[str],
    *,
    project_id: UUID,
    status: str | None = None,
    goal_id: UUID | None = None,
    activity_id: UUID | None = None,
    limit: int = 50,
    after: UUID | None = None,
) -> list[VerificationObligationResource]:
    """按项目列出验证义务；status=QUARANTINED 即 quarantine inbox 可读面。"""
    with engine.connect() as db:
        ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
        rows = db.execute(
            text(
                """SELECT o.* FROM verification_obligations o
                LEFT JOIN activities a ON a.id = o.activity_id
                WHERE o.project_id=:project
                  AND (CAST(:status AS text) IS NULL OR o.status=CAST(:status AS text))
                  AND (CAST(:goal AS uuid) IS NULL OR a.goal_id=CAST(:goal AS uuid))
                  AND (CAST(:activity AS uuid) IS NULL
                       OR o.activity_id=CAST(:activity AS uuid))
                  AND (CAST(:after AS uuid) IS NULL OR (o.created_at,o.id)>(
                    SELECT created_at,id FROM verification_obligations
                    WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY o.created_at, o.id
                LIMIT :limit"""
            ),
            {
                "project": project_id,
                "status": status,
                "goal": goal_id,
                "activity": activity_id,
                "after": after,
                "limit": limit,
            },
        ).mappings()
        return [_obligation_from_row(row) for row in rows]


def get_obligation(
    engine: Engine,
    obligation_id: UUID,
    subject: str,
    project_ids: list[str],
) -> VerificationObligationResource:
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM verification_obligations WHERE id=:id"),
                {"id": obligation_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
        return _obligation_from_row(row)


def count_quarantined_obligations(db, project_id: UUID) -> int:
    """项目内 QUARANTINED 义务计数（SystemStatus 信号）。"""
    return int(
        db.execute(
            text(
                """SELECT COUNT(*) FROM verification_obligations
                WHERE project_id=:p AND status='QUARANTINED'"""
            ),
            {"p": project_id},
        ).scalar_one()
    )


def open_obligations_for_claim(db, activity, attempt_id: UUID) -> list:
    """AUDIT/VALIDATE_SKILL/FINALIZE 且有 assignment 时，每条开一条 OPEN 义务（幂等）。"""
    if activity["kind"] not in _VERIFY_KINDS:
        return []
    # GOAL_REVIEW 是周期诊断，不开 verification obligation，不贡献 PASS
    if activity["target_type"] == "GOAL_REVIEW":
        return []
    assignments = activity["verification_assignments"] or []
    if not assignments:
        return []

    if activity["target_type"] == "CANDIDATE":
        subject_type = "CANDIDATE"
    elif activity["target_type"] == "SKILL_VERSION":
        subject_type = "SKILL_VERSION"
    else:
        raise PlanRejected("验证活动 target_type 无法映射 subject_type")

    subject_id = activity["target_id"]
    rows = []
    for assignment in assignments:
        profile_id = assignment.get("verification_profile_id")
        layer = assignment.get("layer")
        audit_round = assignment.get("audit_round")
        if profile_id is None or layer is None or audit_round is None:
            raise PlanRejected("verification_assignments 缺少 profile/layer/audit_round")
        obligation_id = uuid4()
        db.execute(
            text(
                """INSERT INTO verification_obligations(
                  id,project_id,activity_id,attempt_id,subject_type,subject_id,
                  profile_id,layer,audit_round,status)
                VALUES(
                  :id,:project,:activity,:attempt,:subject_type,:subject_id,
                  :profile,:layer,:round,'OPEN')
                ON CONFLICT (activity_id, attempt_id, subject_id, profile_id, layer, audit_round)
                DO NOTHING"""
            ),
            {
                "id": obligation_id,
                "project": activity["project_id"],
                "activity": activity["id"],
                "attempt": attempt_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "profile": profile_id,
                "layer": layer,
                "round": int(audit_round),
            },
        )
        row = (
            db.execute(
                text(
                    """SELECT * FROM verification_obligations
                    WHERE activity_id=:activity AND attempt_id=:attempt
                      AND subject_id=:subject_id AND profile_id=:profile
                      AND layer=:layer AND audit_round=:round"""
                ),
                {
                    "activity": activity["id"],
                    "attempt": attempt_id,
                    "subject_id": subject_id,
                    "profile": profile_id,
                    "layer": layer,
                    "round": int(audit_round),
                },
            )
            .mappings()
            .one()
        )
        # Issue #23：后继验证 claim 后，SUPERSEDE 同 subject+kind 的旧 QUARANTINED
        supersede_quarantined_after_reverify_claim(
            db,
            subject_id=subject_id,
            profile_id=profile_id,
            layer=layer,
            new_round=int(audit_round),
            new_obligation_id=row["id"],
            activity_kind=activity["kind"],
        )
        row = (
            db.execute(
                text("SELECT * FROM verification_obligations WHERE id=:id"),
                {"id": row["id"]},
            )
            .mappings()
            .one()
        )
        rows.append(row)
    return rows


def require_open_obligation(
    db,
    activity_id: UUID,
    attempt_id: UUID,
    subject_id: UUID,
    profile_id: UUID,
    layer: str,
    audit_round: int,
):
    row = (
        db.execute(
            text(
                """SELECT * FROM verification_obligations
                WHERE activity_id=:activity AND attempt_id=:attempt
                  AND subject_id=:subject_id AND profile_id=:profile
                  AND layer=:layer AND audit_round=:round
                FOR UPDATE"""
            ),
            {
                "activity": activity_id,
                "attempt": attempt_id,
                "subject_id": subject_id,
                "profile": profile_id,
                "layer": layer,
                "round": int(audit_round),
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PlanRejected("缺少 VerificationObligation")
    if row["status"] != "OPEN":
        raise PlanRejected("VerificationObligation 已非 OPEN")
    return row


def assert_obligation_actions_linked_for_run(
    db,
    *,
    activity_id: UUID,
    attempt_id: UUID,
    subject_id: UUID,
    profile_id: UUID,
    layer: str,
    audit_round: int,
) -> None:
    """VerificationRun 发出前：义务须 OPEN、已关联动作，且动作不得处于在途非终态。

    - 空义务：禁止发出（doc/05）
    - PREPARED/AUTHORIZED/DISPATCHED 等在途：禁止发出——否则会先落 assessment
      而 settle 早退，重试走幂等分支永不 settle，造成 OPEN 死锁（Codex/#23）
    - UNKNOWN：允许发出；settle 将义务置 QUARANTINED，对账后走新 audit_round（Issue #23）
    - SUCCEEDED/FAILED/CANCELLED：允许发出并 ASSESSED
    """
    row = require_open_obligation(
        db, activity_id, attempt_id, subject_id, profile_id, layer, audit_round
    )
    effect_ids = list(row["effect_ids"] or [])
    invocation_ids = list(row["invocation_ids"] or [])
    if not effect_ids and not invocation_ids:
        raise PlanRejected("未关联验证动作，禁止发出 VerificationRun")

    if effect_ids:
        statuses = (
            db.execute(
                text("SELECT status FROM effect_intents WHERE id = ANY(:ids)"),
                {"ids": effect_ids},
            )
            .scalars()
            .all()
        )
        if len(statuses) != len(effect_ids):
            raise PlanRejected("验证动作引用缺失，禁止发出 VerificationRun")
        for status in statuses:
            if status in _EFFECT_SETTLED or status == "UNKNOWN":
                continue
            raise PlanRejected(
                "验证动作未结算，禁止发出 VerificationRun"
            )

    if invocation_ids:
        statuses = (
            db.execute(
                text("SELECT status FROM model_invocations WHERE id = ANY(:ids)"),
                {"ids": invocation_ids},
            )
            .scalars()
            .all()
        )
        if len(statuses) != len(invocation_ids):
            raise PlanRejected("验证动作引用缺失，禁止发出 VerificationRun")
        for status in statuses:
            if status in _INVOCATION_SETTLED or status == "UNKNOWN":
                continue
            raise PlanRejected(
                "验证动作未结算，禁止发出 VerificationRun"
            )


def resolve_open_obligation_for_prepare(db, activity, attempt_id: UUID):
    """单 assignment 时自动匹配 OPEN 义务；多 assignment 需显式字段（本切片拒绝）。"""
    assignments = activity["verification_assignments"] or []
    if not assignments:
        raise PlanRejected("缺少 VerificationObligation")
    if len(assignments) > 1:
        raise PlanRejected("多个 VerificationObligation 需显式指定")
    assignment = assignments[0]
    return require_open_obligation(
        db,
        activity["id"],
        attempt_id,
        activity["target_id"],
        UUID(str(assignment["verification_profile_id"])),
        assignment["layer"],
        int(assignment["audit_round"]),
    )


def link_effect(db, obligation_id: UUID, effect_id: UUID) -> None:
    row = (
        db.execute(
            text(
                """SELECT id, status, effect_ids FROM verification_obligations
                WHERE id=:id FOR UPDATE"""
            ),
            {"id": obligation_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PlanRejected("缺少 VerificationObligation")
    if row["status"] != "OPEN":
        raise PlanRejected("仅 OPEN 的 VerificationObligation 可关联 effect")
    existing = list(row["effect_ids"] or [])
    if effect_id in existing:
        return
    db.execute(
        text(
            """UPDATE verification_obligations
            SET effect_ids = array_append(effect_ids, :effect),
                updated_at = clock_timestamp()
            WHERE id=:id AND status='OPEN'"""
        ),
        {"id": obligation_id, "effect": effect_id},
    )


def link_invocation(db, obligation_id: UUID, invocation_id: UUID) -> None:
    row = (
        db.execute(
            text(
                """SELECT id, status, invocation_ids FROM verification_obligations
                WHERE id=:id FOR UPDATE"""
            ),
            {"id": obligation_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PlanRejected("缺少 VerificationObligation")
    if row["status"] != "OPEN":
        raise PlanRejected("仅 OPEN 的 VerificationObligation 可关联 invocation")
    existing = list(row["invocation_ids"] or [])
    if invocation_id in existing:
        return
    db.execute(
        text(
            """UPDATE verification_obligations
            SET invocation_ids = array_append(invocation_ids, :invocation),
                updated_at = clock_timestamp()
            WHERE id=:id AND status='OPEN'"""
        ),
        {"id": obligation_id, "invocation": invocation_id},
    )


def settle_obligation_after_assessment(
    db,
    *,
    activity_id: UUID,
    attempt_id: UUID,
    subject_id: UUID,
    profile_id: UUID,
    layer: str,
    audit_round: int,
    assessment_id: UUID,
) -> None:
    """所列 effect/invocation 均已结算（或均为空）时置 ASSESSED。

    若仍有 UNKNOWN：义务 → QUARANTINED（对账 inbox），禁止 DONE。
    若仍有在途非终态：保持 OPEN。
    """
    row = (
        db.execute(
            text(
                """SELECT * FROM verification_obligations
                WHERE activity_id=:activity AND attempt_id=:attempt
                  AND subject_id=:subject_id AND profile_id=:profile
                  AND layer=:layer AND audit_round=:round
                FOR UPDATE"""
            ),
            {
                "activity": activity_id,
                "attempt": attempt_id,
                "subject_id": subject_id,
                "profile": profile_id,
                "layer": layer,
                "round": int(audit_round),
            },
        )
        .mappings()
        .first()
    )
    if row is None or row["status"] != "OPEN":
        return

    effect_ids = list(row["effect_ids"] or [])
    invocation_ids = list(row["invocation_ids"] or [])
    if effect_ids:
        statuses = (
            db.execute(
                text("SELECT status FROM effect_intents WHERE id = ANY(:ids)"),
                {"ids": effect_ids},
            )
            .scalars()
            .all()
        )
        if len(statuses) != len(effect_ids):
            return
        if any(s == "UNKNOWN" for s in statuses):
            # AB03×义务：未对账副作用进入隔离，不得 ASSESSED / DONE
            db.execute(
                text(
                    """UPDATE verification_obligations
                    SET status='QUARANTINED', updated_at=clock_timestamp()
                    WHERE id=:id AND status='OPEN'"""
                ),
                {"id": row["id"]},
            )
            return
        if any(s not in _EFFECT_SETTLED for s in statuses):
            return
    if invocation_ids:
        statuses = (
            db.execute(
                text("SELECT status FROM model_invocations WHERE id = ANY(:ids)"),
                {"ids": invocation_ids},
            )
            .scalars()
            .all()
        )
        if len(statuses) != len(invocation_ids):
            return
        if any(s == "UNKNOWN" for s in statuses):
            db.execute(
                text(
                    """UPDATE verification_obligations
                    SET status='QUARANTINED', updated_at=clock_timestamp()
                    WHERE id=:id AND status='OPEN'"""
                ),
                {"id": row["id"]},
            )
            return
        if any(s not in _INVOCATION_SETTLED for s in statuses):
            return

    db.execute(
        text(
            """UPDATE verification_obligations
            SET status='ASSESSED', assessment_id=:assessment,
                updated_at=clock_timestamp()
            WHERE id=:id AND status='OPEN'"""
        ),
        {"id": row["id"], "assessment": assessment_id},
    )


def _bump_activity_assignment_round(
    db,
    *,
    activity_id: UUID,
    profile_id: UUID,
    layer: str,
    from_round: int,
    to_round: int,
) -> None:
    """将活动 verification_assignments 中匹配项的 audit_round 递增（供新 VerificationRun）。"""
    import json

    row = (
        db.execute(
            text(
                """SELECT verification_assignments FROM activities
                WHERE id=:id FOR UPDATE"""
            ),
            {"id": activity_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return
    assignments = list(row["verification_assignments"] or [])
    if isinstance(assignments, str):
        assignments = json.loads(assignments)
    changed = False
    for item in assignments:
        if str(item.get("verification_profile_id")) != str(profile_id):
            continue
        if item.get("layer") != layer:
            continue
        if int(item.get("audit_round") or 0) != int(from_round):
            continue
        item["audit_round"] = int(to_round)
        changed = True
    if not changed:
        return
    db.execute(
        text(
            """UPDATE activities
            SET verification_assignments=CAST(:assignments AS jsonb),
                updated_at=clock_timestamp()
            WHERE id=:id"""
        ),
        {"id": activity_id, "assignments": json.dumps(assignments)},
    )


def _schedule_reverify_activity(db, source_activity, *, next_round: int) -> UUID:
    """为已终态验证活动克隆 READY 后继（递增 audit_round）；幂等复用已有 READY。

    Issue #23：binding 按 `_live_binding` 重算（subject/policy/model/skill/goal digest），
    禁止沿用源活动陈旧 binding 导致「digest 已变仍可按旧结论续跑」。
    """
    import json

    from sqlalchemy.exc import NoResultFound

    from ..protocols.runtime import BindingStale
    from .activities import PLAN_RESOURCES
    from .claims import _live_binding

    assignments = list(source_activity["verification_assignments"] or [])
    if isinstance(assignments, str):
        assignments = json.loads(assignments)
    bumped = []
    for item in assignments:
        cloned = dict(item)
        cloned["audit_round"] = int(next_round)
        bumped.append(cloned)

    try:
        live_binding = _live_binding(db, source_activity)
    except (BindingStale, NoResultFound) as exc:
        raise PlanRejected(
            "SUBJECT_DIGEST_UNAVAILABLE: 无法按 live binding 调度后继验证"
        ) from exc
    live_json = json.dumps(live_binding)

    existing = (
        db.execute(
            text(
                """SELECT id, verification_assignments, binding FROM activities
                WHERE goal_id IS NOT DISTINCT FROM :goal
                  AND project_id=:project
                  AND kind=:kind
                  AND target_type=:ttype
                  AND target_id=:tid
                  AND status='READY'
                ORDER BY created_at DESC
                LIMIT 8"""
            ),
            {
                "goal": source_activity["goal_id"],
                "project": source_activity["project_id"],
                "kind": source_activity["kind"],
                "ttype": source_activity["target_type"],
                "tid": source_activity["target_id"],
            },
        )
        .mappings()
        .all()
    )
    for cand in existing:
        assigns = cand["verification_assignments"] or []
        if isinstance(assigns, str):
            assigns = json.loads(assigns)
        rounds = {int(a.get("audit_round") or 0) for a in assigns}
        if int(next_round) in rounds:
            # 幂等复用时仍刷新 binding，吸收调度后的 digest 变化
            db.execute(
                text(
                    """UPDATE activities SET binding=CAST(:binding AS jsonb),
                        updated_at=clock_timestamp()
                    WHERE id=:id AND status='READY'"""
                ),
                {"id": cand["id"], "binding": live_json},
            )
            return cand["id"]

    new_id = uuid4()
    db.execute(
        text(
            """INSERT INTO activities(
              id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
              binding,verification_assignments,status,state_revision,depends_on_activity_ids,
              retry_count,resources)
            VALUES(
              :id,:project,:goal,:task,:scope,:kind,:ttype,:tid,
              CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'READY',1,'{}',0,
              CAST(:resources AS jsonb))"""
        ),
        {
            "id": new_id,
            "project": source_activity["project_id"],
            "goal": source_activity["goal_id"],
            "task": source_activity["task_id"],
            "scope": source_activity["budget_scope_id"],
            "kind": source_activity["kind"],
            "ttype": source_activity["target_type"],
            "tid": source_activity["target_id"],
            "binding": live_json,
            "assignments": json.dumps(bumped),
            "resources": PLAN_RESOURCES.model_dump_json(),
        },
    )
    return new_id


def supersede_quarantined_after_reverify_claim(
    db,
    *,
    subject_id: UUID,
    profile_id: UUID,
    layer: str,
    new_round: int,
    new_obligation_id: UUID,
    effect_ids: list | None = None,
    invocation_ids: list | None = None,
    activity_kind: str | None = None,
) -> None:
    """新 round OPEN 建立后：同 subject（及可选同 kind）较低 round 的 QUARANTINED → SUPERSEDED。"""
    priors = (
        db.execute(
            text(
                """SELECT o.id, o.effect_ids, o.invocation_ids
                FROM verification_obligations o
                JOIN activities a ON a.id = o.activity_id
                WHERE o.subject_id=:subject AND o.profile_id=:profile AND o.layer=:layer
                  AND o.status='QUARANTINED' AND o.audit_round < :round
                  AND (CAST(:kind AS text) IS NULL OR a.kind = CAST(:kind AS text))
                FOR UPDATE OF o"""
            ),
            {
                "subject": subject_id,
                "profile": profile_id,
                "layer": layer,
                "round": int(new_round),
                "kind": activity_kind,
            },
        )
        .mappings()
        .all()
    )
    merged_effects = list(effect_ids or [])
    merged_invocations = list(invocation_ids or [])
    for prior in priors:
        for eid in prior["effect_ids"] or []:
            if eid not in merged_effects:
                merged_effects.append(eid)
        for iid in prior["invocation_ids"] or []:
            if iid not in merged_invocations:
                merged_invocations.append(iid)
        db.execute(
            text(
                """UPDATE verification_obligations
                SET status='SUPERSEDED',
                    superseded_by_obligation_id=:succ,
                    updated_at=clock_timestamp()
                WHERE id=:id AND status='QUARANTINED'"""
            ),
            {"id": prior["id"], "succ": new_obligation_id},
        )
    if merged_effects or merged_invocations:
        db.execute(
            text(
                """UPDATE verification_obligations
                SET effect_ids=:effects, invocation_ids=:invocations,
                    updated_at=clock_timestamp()
                WHERE id=:id AND status='OPEN'"""
            ),
            {
                "id": new_obligation_id,
                "effects": merged_effects,
                "invocations": merged_invocations,
            },
        )


def reevaluate_quarantined_obligations_for_effect(db, effect_id: UUID) -> None:
    """对账解除 UNKNOWN 后推进验证生命周期（Issue #23）。

    - 源验证 Activity 仍 RUNNING：同 attempt 开 audit_round+1 OPEN，旧 → SUPERSEDED
    - 源已终态：调度 READY 后继验证 Activity（递增 round），旧保持 QUARANTINED 挡 DONE；
      待后继 claim 开立新 OPEN 后再 SUPERSEDED（见 supersede_quarantined_after_reverify_claim）
    """
    rows = (
        db.execute(
            text(
                """SELECT * FROM verification_obligations
                WHERE status='QUARANTINED' AND :effect = ANY(effect_ids)
                FOR UPDATE"""
            ),
            {"effect": effect_id},
        )
        .mappings()
        .all()
    )
    for row in rows:
        effect_ids = list(row["effect_ids"] or [])
        invocation_ids = list(row["invocation_ids"] or [])
        if effect_ids:
            statuses = (
                db.execute(
                    text("SELECT status FROM effect_intents WHERE id = ANY(:ids)"),
                    {"ids": effect_ids},
                )
                .scalars()
                .all()
            )
            if len(statuses) != len(effect_ids) or any(
                s not in _EFFECT_SETTLED for s in statuses
            ):
                continue
        if invocation_ids:
            statuses = (
                db.execute(
                    text("SELECT status FROM model_invocations WHERE id = ANY(:ids)"),
                    {"ids": invocation_ids},
                )
                .scalars()
                .all()
            )
            if len(statuses) != len(invocation_ids) or any(
                s not in _INVOCATION_SETTLED for s in statuses
            ):
                continue

        next_round = int(row["audit_round"]) + 1
        activity = (
            db.execute(
                text("SELECT * FROM activities WHERE id=:id FOR UPDATE"),
                {"id": row["activity_id"]},
            )
            .mappings()
            .first()
        )
        if activity is None:
            continue

        if activity["status"] == "RUNNING":
            # 同 attempt 后继 OPEN（源仍可交 VerificationRun）
            successor = (
                db.execute(
                    text(
                        """SELECT id FROM verification_obligations
                        WHERE activity_id=:activity AND attempt_id=:attempt
                          AND subject_id=:subject AND profile_id=:profile
                          AND layer=:layer AND audit_round=:round
                        FOR UPDATE"""
                    ),
                    {
                        "activity": row["activity_id"],
                        "attempt": row["attempt_id"],
                        "subject": row["subject_id"],
                        "profile": row["profile_id"],
                        "layer": row["layer"],
                        "round": next_round,
                    },
                )
                .mappings()
                .first()
            )
            if successor is None:
                new_id = uuid4()
                db.execute(
                    text(
                        """INSERT INTO verification_obligations(
                          id,project_id,activity_id,attempt_id,subject_type,subject_id,
                          profile_id,layer,audit_round,effect_ids,invocation_ids,status)
                        VALUES(
                          :id,:project,:activity,:attempt,:subject_type,:subject,
                          :profile,:layer,:round,:effects,:invocations,'OPEN')"""
                    ),
                    {
                        "id": new_id,
                        "project": row["project_id"],
                        "activity": row["activity_id"],
                        "attempt": row["attempt_id"],
                        "subject_type": row["subject_type"],
                        "subject": row["subject_id"],
                        "profile": row["profile_id"],
                        "layer": row["layer"],
                        "round": next_round,
                        "effects": effect_ids,
                        "invocations": invocation_ids,
                    },
                )
                successor_id = new_id
                _bump_activity_assignment_round(
                    db,
                    activity_id=row["activity_id"],
                    profile_id=row["profile_id"],
                    layer=row["layer"],
                    from_round=int(row["audit_round"]),
                    to_round=next_round,
                )
            else:
                successor_id = successor["id"]

            db.execute(
                text(
                    """UPDATE verification_obligations
                    SET status='SUPERSEDED',
                        superseded_by_obligation_id=:succ,
                        updated_at=clock_timestamp()
                    WHERE id=:id AND status='QUARANTINED'"""
                ),
                {"id": row["id"], "succ": successor_id},
            )
            continue

        # 仅明确终态 + 验证类：调度 READY 后继；旧 QUARANTINED 继续挡 DONE
        # RECOVERING/READY/WAITING 等失租恢复态本批 no-op，避免并行后继分叉
        if (
            activity["status"] in _VERIFY_TERMINAL
            and activity["kind"] in _VERIFY_KINDS
        ):
            _schedule_reverify_activity(db, activity, next_round=next_round)


def assert_goal_ready_for_done(db, goal_id: UUID) -> None:
    """Goal DONE 前：无未决副作用、无未确认 Stop/隔离资源。

    与义务门禁正交；任一失败关闭，禁止假 DONE。
    """
    unsettled = db.execute(
        text(
            """SELECT 1 FROM effect_intents e
            JOIN activities a ON a.id = e.activity_id
            WHERE a.goal_id = :goal
              AND e.status IN ('PREPARED','AUTHORIZED','DISPATCHED','UNKNOWN')
            LIMIT 1"""
        ),
        {"goal": goal_id},
    ).first()
    if unsettled is not None:
        raise PlanRejected(
            "GOAL_DONE_BLOCKED_UNSETTLED_EFFECTS: 存在未决 effect，禁止 Goal DONE"
        )
    from .audits import goal_has_blocking_review
    from .stops import goal_has_unconfirmed_stop_barrier

    if goal_has_unconfirmed_stop_barrier(db, goal_id):
        raise PlanRejected(
            "STOP_UNCONFIRMED: 存在未确认 Stop 或隔离资源，禁止 Goal DONE"
        )
    if goal_has_blocking_review(db, goal_id):
        raise PlanRejected(
            "GOAL_REVIEW_BLOCKER: 最新 GoalReview 含 BLOCKER，禁止 Goal DONE"
        )


def _live_subject_content_digest(db, *, subject_type: str, subject_id: UUID) -> str:
    """当前封存 subject 的 content_digest；缺失则失败关闭。"""
    if subject_type == "CANDIDATE":
        digest = db.execute(
            text("SELECT content_digest FROM candidate_manifests WHERE id=:id"),
            {"id": subject_id},
        ).scalar()
        if digest is None:
            raise PlanRejected("SUBJECT_DIGEST_UNAVAILABLE: 候选不存在，禁止 Goal DONE")
        return str(digest)
    if subject_type == "SKILL_VERSION":
        digest = db.execute(
            text("SELECT content_digest FROM skill_versions WHERE id=:id"),
            {"id": subject_id},
        ).scalar()
        if digest is None:
            raise PlanRejected(
                "SUBJECT_DIGEST_UNAVAILABLE: 技能版本不存在，禁止 Goal DONE"
            )
        return str(digest)
    raise PlanRejected(f"未知 subject_type: {subject_type}")


def _assert_assessment_binding_fresh(db, *, assessment_id: UUID) -> None:
    """Issue #23 TDD.6：ASSESSED 结论须与 live subject / profile verifier 一致。

    trust_revision 由调用方另行核对；此处专责 subject_digest 与 profile verifier_digest。
    evidence 失效经 TrustState.trust_revision 推进（见 assert_* 的 trust 缝）。
    """
    row = (
        db.execute(
            text(
                """SELECT a.subject_type, a.subject_id, a.subject_digest AS assessed_subject,
                          a.verification_profile_id,
                          r.subject_digest AS run_subject, r.verifier_digest AS run_verifier,
                          p.verifier_digest AS profile_verifier
                FROM verification_assessments a
                JOIN verification_runs r ON r.id = a.run_id
                JOIN verification_profiles p
                  ON p.id = a.verification_profile_id AND p.project_id = a.project_id
                WHERE a.id=:id"""
            ),
            {"id": assessment_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PlanRejected("assessment 不存在")
    live = _live_subject_content_digest(
        db, subject_type=row["subject_type"], subject_id=row["subject_id"]
    )
    if row["assessed_subject"] != live or row["run_subject"] != live:
        raise PlanRejected(
            "SUBJECT_DIGEST_STALE: 已评估义务的 subject_digest 已过期，须重新验收"
        )
    if row["run_verifier"] != row["profile_verifier"]:
        raise PlanRejected(
            "PROFILE_DIGEST_STALE: 已评估义务的 verifier/profile 已过期，须重新验收"
        )


def assert_no_pending_obligations(
    db, *, subject_id: UUID, exclude_activity_id: UUID | None = None
) -> None:
    """Task/Goal DONE 前：subject 上不得残留 OPEN/QUARANTINED 义务。

    Issue #23：ASSESSED 但 trust_revision / subject / profile 已漂移视同未失效关闭，
    禁止用陈旧评估冒充可 DONE。
    """
    if exclude_activity_id is None:
        pending = db.execute(
            text(
                """SELECT 1 FROM verification_obligations
                WHERE subject_id=:subject AND status IN ('OPEN','QUARANTINED')
                LIMIT 1"""
            ),
            {"subject": subject_id},
        ).first()
    else:
        pending = db.execute(
            text(
                """SELECT 1 FROM verification_obligations
                WHERE subject_id=:subject AND status IN ('OPEN','QUARANTINED')
                  AND activity_id<>:exclude
                LIMIT 1"""
            ),
            {"subject": subject_id, "exclude": exclude_activity_id},
        ).first()
    if pending is not None:
        raise PlanRejected("存在未结算的 VerificationObligation")

    stale = db.execute(
        text(
            """SELECT 1 FROM verification_obligations o
            JOIN verification_assessments a ON a.id = o.assessment_id
            JOIN project_trust_states t ON t.project_id = o.project_id
            WHERE o.subject_id=:subject AND o.status='ASSESSED'
              AND (
                t.status <> 'OPEN'
                OR a.trust_revision <> t.trust_revision
              )
            LIMIT 1"""
        ),
        {"subject": subject_id},
    ).first()
    if stale is not None:
        raise PlanRejected(
            "TRUST_REVISION_STALE: 已评估义务的 trust_revision 已过期或信任已封锁"
        )

    assessed = db.execute(
        text(
            """SELECT assessment_id FROM verification_obligations
            WHERE subject_id=:subject AND status='ASSESSED' AND assessment_id IS NOT NULL"""
        ),
        {"subject": subject_id},
    ).scalars()
    for assessment_id in assessed:
        _assert_assessment_binding_fresh(db, assessment_id=assessment_id)


def assert_attempt_obligations_assessed(
    db, *, activity_id: UUID, attempt_id: UUID
) -> None:
    """FINALIZE PASS：本 attempt 义务必须存在且现行义务全部 ASSESSED。

    Issue #23：SUPERSEDED 为历史轮次，不挡收敛；OPEN/QUARANTINED 仍失败关闭。
    ASSESSED 所绑 assessment 的 trust_revision / subject / profile
    须与项目当前一致；漂移则旧结论失效，禁止冒充可 DONE。
    """
    rows = (
        db.execute(
            text(
                """SELECT id, status, assessment_id, project_id FROM verification_obligations
                WHERE activity_id=:activity AND attempt_id=:attempt"""
            ),
            {"activity": activity_id, "attempt": attempt_id},
        )
        .mappings()
        .all()
    )
    if not rows:
        raise PlanRejected("缺少 VerificationObligation")
    if any(row["status"] in ("OPEN", "QUARANTINED") for row in rows):
        raise PlanRejected("VerificationObligation 尚未结算")
    assessed_rows = [row for row in rows if row["status"] == "ASSESSED"]
    if not assessed_rows:
        raise PlanRejected("缺少 VerificationObligation")

    project_id = assessed_rows[0]["project_id"]
    trust = (
        db.execute(
            text(
                """SELECT status, trust_revision FROM project_trust_states
                WHERE project_id=:id FOR SHARE"""
            ),
            {"id": project_id},
        )
        .mappings()
        .one()
    )
    if trust["status"] != "OPEN":
        raise PlanRejected(
            "TRUST_BLOCKED: 项目信任已封锁，已评估义务结论失效，禁止 Goal DONE"
        )
    current_rev = int(trust["trust_revision"])
    for row in assessed_rows:
        if row["assessment_id"] is None:
            raise PlanRejected("ASSESSED 义务缺少 assessment_id")
        assessed_rev = db.execute(
            text(
                """SELECT trust_revision FROM verification_assessments
                WHERE id=:id"""
            ),
            {"id": row["assessment_id"]},
        ).scalar()
        if assessed_rev is None:
            raise PlanRejected("assessment 不存在")
        if int(assessed_rev) != current_rev:
            raise PlanRejected(
                "TRUST_REVISION_STALE: 义务评估时的 trust_revision 已过期，须重新验收"
            )
        _assert_assessment_binding_fresh(db, assessment_id=row["assessment_id"])
