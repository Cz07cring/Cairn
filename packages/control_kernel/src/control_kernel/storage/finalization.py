"""最终屏障：Task 全集 DONE → VERIFYING → SEALED FINALIZE → ReleaseManifest → Goal DONE。

诚实边界：无 INTEGRATE Activity；以 Goal 下最新候选作为最终集成候选。
本切片要求 Goal 至少一条 GOAL/GLOBAL success_criterion，否则不开启屏障。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..protocols.finalization import (
    FinalizationRecoveryRequest,
    FinalizeSuccessOutcome,
    ReleaseManifestResource,
    ReleaseValidity,
    ReleaseView,
)
from ..protocols.goals import BarrierResource
from ..protocols.plans import ActivityOutcomeRequest
from ..protocols.runtime import (
    ActivityResource,
    CommandOperation,
    CommandResult,
    ExecutionBinding,
    InvalidGoalState,
    LeaseRejected,
    PlanRejected,
    Resources,
    StateRevisionConflict,
    WorkerForbidden,
)
from .activities import PLAN_RESOURCES, _command_from_row
from .events import append_goal_event
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .probe import _content_digest
from .projects import ProjectConflict
from .verification_runs import require_matching_verifier_runs


def _barrier_from_row(row) -> BarrierResource:
    data = dict(row)
    return BarrierResource.model_validate(
        {
            "id": data["id"],
            "goal_id": data["goal_id"],
            "write_epoch": data["write_epoch"],
            "status": data["status"],
            "contract_revision": data["contract_revision"],
            "plan_revision": data["plan_revision"],
            "candidate_manifest_id": data["candidate_manifest_id"],
            "in_flight_engineering": data["in_flight_engineering"],
            "unknown_effects": data["unknown_effects"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
        }
    )


def _release_from_row(row) -> ReleaseManifestResource:
    data = dict(row)
    return ReleaseManifestResource.model_validate(
        {
            **data,
            "verification_profile_ids": list(data["verification_profile_ids"] or []),
            "audit_ids": list(data["audit_ids"] or []),
            "evidence_ids": list(data["evidence_ids"] or []),
        }
    )


def get_barrier_for_goal(db: Connection, goal_id: UUID) -> BarrierResource | None:
    row = (
        db.execute(
            text("""SELECT * FROM finalization_barriers
            WHERE goal_id=:goal AND status IN ('DRAINING','SEALED','RELEASED')
            ORDER BY created_at DESC LIMIT 1"""),
            {"goal": goal_id},
        )
        .mappings()
        .first()
    )
    return _barrier_from_row(row) if row else None


def _count_engineering_flight(db: Connection, goal_id: UUID) -> tuple[int, int]:
    """在途 ENGINEERING（PREPARED/AUTHORIZED/DISPATCHED）与 UNKNOWN 计数。"""
    in_flight = db.execute(
        text("""SELECT count(*) FROM effect_intents e
        JOIN activities a ON a.id=e.activity_id
        WHERE a.goal_id=:goal
          AND e.scope='ENGINEERING'
          AND e.status IN ('PREPARED','AUTHORIZED','DISPATCHED')"""),
        {"goal": goal_id},
    ).scalar_one()
    unknown = db.execute(
        text("""SELECT count(*) FROM effect_intents e
        JOIN activities a ON a.id=e.activity_id
        WHERE a.goal_id=:goal AND e.status='UNKNOWN'"""),
        {"goal": goal_id},
    ).scalar_one()
    return int(in_flight), int(unknown)


def _global_assignments_for_goal(db: Connection, goal) -> list[dict]:
    """Goal 合同上 GOAL/GLOBAL 标准 → FINALIZE verification_assignments。"""
    criteria = goal["contract"]["success_criteria"]
    required = [c for c in criteria if c.get("required")]
    global_assignments = []
    for criterion in required:
        profile = (
            db.execute(
                text("""SELECT id, content_digest, config
                FROM verification_profiles WHERE id=:id"""),
                {"id": criterion["verification_profile_id"]},
            )
            .mappings()
            .first()
        )
        if profile is None:
            continue
        cfg = profile["config"] or {}
        if cfg.get("target_scope") != "GOAL" or list(cfg.get("required_layers") or []) != [
            "GLOBAL"
        ]:
            continue
        global_assignments.append(
            {
                "verification_profile_id": str(profile["id"]),
                "profile_digest": profile["content_digest"],
                "layer": "GLOBAL",
                "audit_round": 1,
                "criterion_id": criterion["id"],
                # 校验器身份随分配下发：Runner 只持 worker 身份，无权读公共
                # /api/v1/verification-profiles（实测 403）。见 candidates.py 同处注释。
                "verifier_digest": (profile.get("config") or {}).get("verifier_digest"),
            }
        )
    return global_assignments


def _insert_finalize_activity(
    db: Connection,
    *,
    goal,
    candidate,
    global_assignments: list[dict],
) -> None:
    """SEALED 后创建 READY FINALIZE；同候选已有未终态 FINALIZE 则跳过（幂等）。"""
    existing = db.execute(
        text(
            """SELECT 1 FROM activities
            WHERE goal_id=:goal AND kind='FINALIZE' AND target_id=:candidate
              AND status IN ('READY','RUNNING','WAITING','RECOVERING')
            LIMIT 1"""
        ),
        {"goal": goal["id"], "candidate": candidate["id"]},
    ).first()
    if existing is not None:
        return
    policy_digest = db.execute(
        text("SELECT content_digest FROM policies WHERE id=:id"),
        {"id": goal["contract"]["policy_id"]},
    ).scalar_one()
    model_digest = db.execute(
        text("SELECT content_digest FROM model_profiles WHERE id=:id"),
        {"id": goal["contract"]["model_profile_id"]},
    ).scalar_one()
    skill_digest = db.execute(
        text("SELECT content_digest FROM skill_sets WHERE id=:id"),
        {"id": goal["contract"]["skill_set_id"]},
    ).scalar_one()
    binding = ExecutionBinding(
        goal_contract_revision=goal["contract_revision"],
        goal_contract_digest=goal["contract_digest"],
        task_contract_revision=None,
        task_contract_digest=None,
        plan_revision=goal["plan_revision"],
        subject_digest=candidate["content_digest"],
        policy_digest=policy_digest,
        model_profile_digest=model_digest,
        skill_set_digest=skill_digest,
    )
    resources = Resources(
        cpu_millicores=100,
        memory_bytes=268435456,
        disk_bytes=67108864,
        model_slots=0,
        browser_slots=0,
        exclusive_labels=[],
    )
    # 审计器（Broker auditor suite）要求分配里带 `subject_digest`：它据此确认「审的是哪份
    # 候选」。缺它时 Runner 侧抛 MISSING_SUBJECT_DIGEST，FINALIZE 活动永停 RUNNING。
    # 语义与 binding.subject_digest 同源（见上方 binding 构造）。
    assignments_with_subject = [
        {**assignment, "subject_digest": candidate["content_digest"]}
        for assignment in global_assignments
    ]
    db.execute(
        text("""INSERT INTO activities(
          id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
          binding,verification_assignments,status,state_revision,depends_on_activity_ids,
          retry_count,resources)
        VALUES(
          :id,:project,:goal,NULL,:goal,'FINALIZE','CANDIDATE',:candidate,
          CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'READY',1,'{}',0,
          CAST(:resources AS jsonb))"""),
        {
            "id": uuid4(),
            "project": goal["project_id"],
            "goal": goal["id"],
            "candidate": candidate["id"],
            "binding": binding.model_dump_json(),
            "assignments": json.dumps(assignments_with_subject),
            "resources": resources.model_dump_json(),
        },
    )


def try_seal_draining_barrier(db: Connection, goal_id: UUID) -> bool:
    """doc/01 §9：DRAINING 排空后晋升 SEALED 并创建 FINALIZE；≠ Goal DONE。

    条件：在途工程=0、无 UNKNOWN、无未确认 Stop/隔离资源、无 GoalReview BLOCKER、
    无未复盘 NO_PROGRESS_STOP。
    已 SEALED / 无 DRAINING 屏障 → 无副作用 False。
    """
    from .activation_terminations import goal_has_unresolved_no_progress_stop
    from .audits import goal_has_blocking_review
    from .stops import goal_has_unconfirmed_stop_barrier

    goal = (
        db.execute(text("SELECT * FROM goals WHERE id=:id FOR UPDATE"), {"id": goal_id})
        .mappings()
        .first()
    )
    if goal is None or goal["status"] != "VERIFYING":
        return False
    barrier = (
        db.execute(
            text(
                """SELECT * FROM finalization_barriers
                WHERE goal_id=:goal AND status='DRAINING'
                ORDER BY created_at DESC LIMIT 1 FOR UPDATE"""
            ),
            {"goal": goal_id},
        )
        .mappings()
        .first()
    )
    if barrier is None:
        return False
    in_flight, unknown = _count_engineering_flight(db, goal_id)
    # 刷新计数（排空过程可观测）
    db.execute(
        text(
            """UPDATE finalization_barriers
            SET in_flight_engineering=:flight, unknown_effects=:unknown,
                updated_at=clock_timestamp()
            WHERE id=:id"""
        ),
        {"id": barrier["id"], "flight": in_flight, "unknown": unknown},
    )
    if (
        in_flight != 0
        or unknown != 0
        or goal_has_unconfirmed_stop_barrier(db, goal_id)
        or goal_has_blocking_review(db, goal_id)
        or goal_has_unresolved_no_progress_stop(db, goal_id)
    ):
        return False
    global_assignments = _global_assignments_for_goal(db, goal)
    if not global_assignments:
        return False
    candidate = (
        db.execute(
            text("""SELECT id, content_digest FROM candidate_manifests
            WHERE id=:id AND goal_id=:goal"""),
            {"id": barrier["candidate_manifest_id"], "goal": goal_id},
        )
        .mappings()
        .first()
    )
    if candidate is None:
        return False
    db.execute(
        text(
            """UPDATE finalization_barriers SET status='SEALED',
                in_flight_engineering=0, unknown_effects=0,
                updated_at=clock_timestamp()
            WHERE id=:id AND status='DRAINING'"""
        ),
        {"id": barrier["id"]},
    )
    _insert_finalize_activity(
        db,
        goal=goal,
        candidate=candidate,
        global_assignments=global_assignments,
    )
    return True


def open_finalization_barrier(
    db: Connection, goal_id: UUID, candidate_manifest_id: UUID
) -> None:
    """INTEGRATE 成功后：Goal→VERIFYING，创建屏障；无在途工程则 SEAL + FINALIZE。"""
    goal = (
        db.execute(text("SELECT * FROM goals WHERE id=:id FOR UPDATE"), {"id": goal_id})
        .mappings()
        .first()
    )
    if goal is None or goal["status"] != "RUNNING":
        return

    global_assignments = _global_assignments_for_goal(db, goal)
    if not global_assignments:
        return

    candidate = (
        db.execute(
            text("""SELECT id, content_digest FROM candidate_manifests
            WHERE id=:id AND goal_id=:goal"""),
            {"id": candidate_manifest_id, "goal": goal_id},
        )
        .mappings()
        .first()
    )
    if candidate is None:
        return

    new_epoch = str(int(goal["write_epoch"]) + 1)
    db.execute(
        text("""UPDATE goals SET status='VERIFYING', previous_status='RUNNING',
          write_epoch=:epoch, state_revision=state_revision+1, updated_at=clock_timestamp()
        WHERE id=:id"""),
        {"id": goal_id, "epoch": new_epoch},
    )

    in_flight, unknown = _count_engineering_flight(db, goal_id)

    barrier_id = uuid4()
    status = "DRAINING"
    from .activation_terminations import goal_has_unresolved_no_progress_stop
    from .audits import goal_has_blocking_review
    from .stops import goal_has_unconfirmed_stop_barrier

    # AB09：未确认 Stop / 隔离资源时不得 SEAL 最终屏障
    # GoalReview BLOCKER：同样不得 SEAL（≠DONE；不改 Goal 为 BLOCKED）
    # M4：未复盘 NO_PROGRESS_STOP 同样不得 SEAL（≠DONE）
    if (
        in_flight == 0
        and unknown == 0
        and not goal_has_unconfirmed_stop_barrier(db, goal_id)
        and not goal_has_blocking_review(db, goal_id)
        and not goal_has_unresolved_no_progress_stop(db, goal_id)
    ):
        status = "SEALED"
    db.execute(
        text("""INSERT INTO finalization_barriers(
          id,project_id,goal_id,write_epoch,status,contract_revision,plan_revision,
          candidate_manifest_id,in_flight_engineering,unknown_effects)
        VALUES(
          :id,:project,:goal,:epoch,:status,:crev,:prev,:candidate,:flight,:unknown)"""),
        {
            "id": barrier_id,
            "project": goal["project_id"],
            "goal": goal_id,
            "epoch": new_epoch,
            "status": status,
            "crev": goal["contract_revision"],
            "prev": goal["plan_revision"],
            "candidate": candidate["id"],
            "flight": in_flight,
            "unknown": unknown,
        },
    )
    if status != "SEALED":
        return

    _insert_finalize_activity(
        db,
        goal={**goal, "id": goal_id, "write_epoch": new_epoch, "status": "VERIFYING"},
        candidate=candidate,
        global_assignments=global_assignments,
    )


# 兼容旧名：测试/外部若仍引用。
def maybe_open_finalization(db: Connection, goal_id: UUID) -> None:
    """已废弃直接开屏障；改由 INTEGRATE 成功后调用 open_finalization_barrier。"""
    return


def submit_finalize_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if not isinstance(body.outcome, FinalizeSuccessOutcome):
        raise PlanRejected("FINALIZE outcome 形状无效")
    outcome = body.outcome
    with engine.begin() as db:
        worker = (
            db.execute(
                text("SELECT id FROM workers WHERE subject=:subject AND status='ACTIVE'"),
                {"subject": subject},
            )
            .mappings()
            .first()
        )
        if worker is None:
            raise WorkerForbidden()
        if body.lease.activity_id != activity_id:
            raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
        # Issue #22：先 peek goal_id 取 admission，再锁 activity/attempt（锁序固定）
        peek = (
            db.execute(
                text("SELECT goal_id, kind FROM activities WHERE id=:id"),
                {"id": activity_id},
            )
            .mappings()
            .first()
        )
        if peek is None:
            raise ScopeNotFound()
        if peek["kind"] != "FINALIZE":
            raise PlanRejected("非 FINALIZE 活动")
        if peek["goal_id"] is None:
            raise PlanRejected("FINALIZE 缺少 goal_id")
        from .goals import acquire_goal_admission_lock
        from .stops import assert_goal_allows_success_outcome

        acquire_goal_admission_lock(db, peek["goal_id"])
        assert_goal_allows_success_outcome(db, peek["goal_id"])
        activity = (
            db.execute(
                text("SELECT * FROM activities WHERE id=:id FOR UPDATE"),
                {"id": activity_id},
            )
            .mappings()
            .first()
        )
        if activity is None:
            raise ScopeNotFound()
        if activity["kind"] != "FINALIZE":
            raise PlanRejected("非 FINALIZE 活动")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if activity["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        attempt = (
            db.execute(
                text("""SELECT * FROM activity_attempts
                WHERE id=:id AND activity_id=:activity FOR UPDATE"""),
                {"id": body.lease.attempt_id, "activity": activity_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise ScopeNotFound()
        if attempt["worker_id"] != worker["id"]:
            raise WorkerForbidden()
        if str(attempt["fencing_epoch"]) != body.lease.fencing_epoch:
            raise LeaseRejected("FENCING_REJECTED", "fencing epoch 不匹配")
        if attempt["status"] != "ACTIVE":
            raise LeaseRejected("INVALID_STATE", "attempt 已非 ACTIVE")
        now = datetime.now(UTC)
        expires = attempt["lease_expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能复活")

        goal = (
            db.execute(
                text("SELECT * FROM goals WHERE id=:id FOR UPDATE"),
                {"id": activity["goal_id"]},
            )
            .mappings()
            .one()
        )
        if goal["status"] != "VERIFYING":
            raise PlanRejected("Goal 不在 VERIFYING")
        barrier = (
            db.execute(
                text("""SELECT * FROM finalization_barriers
                WHERE id=:id AND goal_id=:goal FOR UPDATE"""),
                {"id": outcome.barrier_id, "goal": goal["id"]},
            )
            .mappings()
            .first()
        )
        if barrier is None:
            raise PlanRejected("屏障不存在")
        if barrier["status"] != "SEALED":
            raise PlanRejected("屏障未 SEALED")
        if barrier["write_epoch"] != goal["write_epoch"]:
            raise PlanRejected("write_epoch 与屏障不一致")
        if outcome.candidate_manifest_id != barrier["candidate_manifest_id"]:
            raise PlanRejected("候选与屏障不一致")
        if outcome.candidate_manifest_id != activity["target_id"]:
            raise PlanRejected("候选与活动 target 不一致")

        assignments = activity["verification_assignments"] or []
        if len(outcome.global_audits) != len(assignments):
            raise PlanRejected("GLOBAL 审计条数与分配不一致")
        by_profile = {str(a.verification_profile_id): a for a in outcome.global_audits}
        if len(by_profile) != len(outcome.global_audits):
            raise PlanRejected("GLOBAL 审计 profile 重复")

        # audits.activity_id 唯一：本切片 FINALIZE 仅支持单条 GLOBAL 分配。
        if len(assignments) != 1:
            raise PlanRejected("当前仅支持单条 GLOBAL 最终审计分配")

        assignment = assignments[0]
        audit = by_profile.get(str(assignment["verification_profile_id"]))
        if audit is None:
            raise PlanRejected("缺少分配的 GLOBAL profile 审计")
        if audit.layer != "GLOBAL":
            raise PlanRejected("layer 必须为 GLOBAL")
        if int(audit.audit_round) != int(assignment["audit_round"]):
            raise PlanRejected("audit_round 与分配不一致")
        if audit.subject_candidate_manifest_id != outcome.candidate_manifest_id:
            raise PlanRejected("审计候选不一致")
        if audit.goal_contract_revision != goal["contract_revision"]:
            raise PlanRejected("goal_contract_revision 不一致")
        if audit.verdict not in ("PASS", "FAIL", "INSUFFICIENT"):
            raise PlanRejected("GLOBAL 审计 verdict 无效")
        if audit.verdict == "PASS" and any(
            c.verdict != "PASS" for c in audit.criterion_results
        ):
            raise PlanRejected("GLOBAL 审计准则未全部 PASS")
        if audit.verdict == "FAIL" and not any(
            c.verdict == "FAIL" for c in audit.criterion_results
        ):
            raise PlanRejected("FAIL 审计缺少失败准则")
        criterion_id = assignment.get("criterion_id")
        if criterion_id and criterion_id not in {c.criterion_id for c in audit.criterion_results}:
            raise PlanRejected("缺少必要 GLOBAL criterion")

        # 严格：GLOBAL 审计引用的 VerificationRun 须由本 FINALIZE lease 登记且与候选/分配对齐。
        require_matching_verifier_runs(
            db,
            run_ids=list(audit.verifier_run_ids),
            producer_activity_id=activity_id,
            producer_attempt_id=attempt["id"],
            subject_type="CANDIDATE",
            subject_id=outcome.candidate_manifest_id,
            verification_profile_id=audit.verification_profile_id,
            layer=str(audit.layer),
            audit_round=int(audit.audit_round),
            require_assessment_pass=audit.verdict == "PASS",
        )

        content = {
            "producer_activity_id": str(activity_id),
            "producer_attempt_id": str(attempt["id"]),
            "subject_candidate_manifest_id": str(audit.subject_candidate_manifest_id),
            "goal_contract_revision": audit.goal_contract_revision,
            "task_contract_revision": audit.task_contract_revision,
            "verification_profile_id": str(audit.verification_profile_id),
            "layer": audit.layer,
            "audit_round": audit.audit_round,
            "verifier_run_ids": sorted(str(i) for i in audit.verifier_run_ids),
            "verdict": audit.verdict,
            "criterion_results": sorted(
                (c.model_dump(mode="json") for c in audit.criterion_results),
                key=lambda item: item["criterion_id"],
            ),
            "evidence_ids": sorted(str(i) for i in audit.evidence_ids),
            "reason": audit.reason,
        }
        digest = _content_digest("Audit", content)
        audit_id = uuid4()
        db.execute(
            text("""INSERT INTO audits(
              id,project_id,goal_id,task_id,activity_id,attempt_id,
              subject_candidate_manifest_id,goal_contract_revision,task_contract_revision,
              verification_profile_id,layer,audit_round,verifier_run_ids,verdict,
              criterion_results,evidence_ids,reason,content_digest)
            VALUES(
              :id,:project,:goal,NULL,:activity,:attempt,
              :candidate,:grev,:trev,
              :profile,:layer,:round,:runs,:verdict,
              CAST(:results AS jsonb),:evidence,:reason,:digest)"""),
            {
                "id": audit_id,
                "project": activity["project_id"],
                "goal": activity["goal_id"],
                "activity": activity_id,
                "attempt": attempt["id"],
                "candidate": audit.subject_candidate_manifest_id,
                "grev": audit.goal_contract_revision,
                "trev": audit.task_contract_revision,
                "profile": audit.verification_profile_id,
                "layer": audit.layer,
                "round": audit.audit_round,
                "runs": audit.verifier_run_ids,
                "verdict": audit.verdict,
                "results": json.dumps(
                    [c.model_dump(mode="json") for c in audit.criterion_results]
                ),
                "evidence": audit.evidence_ids,
                "reason": audit.reason,
                "digest": digest,
            },
        )

        # 非 PASS：受理审计后 Goal BLOCKED，屏障保持 SEALED，供 RECOVER_FINALIZATION。
        if audit.verdict != "PASS":
            reason = (
                "FINALIZATION_FAIL"
                if audit.verdict == "FAIL"
                else "FINALIZATION_INSUFFICIENT"
            )
            db.execute(
                text("""UPDATE goals SET status='BLOCKED', previous_status='VERIFYING',
                  block_reason=:reason, state_revision=state_revision+1,
                  updated_at=clock_timestamp()
                  WHERE id=:id"""),
                {"id": goal["id"], "reason": reason},
            )
            db.execute(
                text("""UPDATE activity_attempts SET status='COMPLETED', finished_at=:now,
                  updated_at=clock_timestamp() WHERE id=:id"""),
                {"id": attempt["id"], "now": now},
            )
            db.execute(
                text("""UPDATE resource_reservations SET status='RELEASED', updated_at=clock_timestamp()
                  WHERE attempt_id=:id AND status='HELD'"""),
                {"id": attempt["id"]},
            )
            db.execute(
                text("""UPDATE budget_reservations SET status='RELEASED', updated_at=clock_timestamp()
                  WHERE attempt_id=:id AND status='HELD'"""),
                {"id": attempt["id"]},
            )
            # 关闭同 Goal 其他 READY FINALIZE，避免并行终验。
            db.execute(
                text(
                    """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                    WHERE goal_id=:goal AND kind='FINALIZE' AND status='READY'
                      AND id<>:id"""
                ),
                {"goal": goal["id"], "id": activity_id},
            )
            updated = (
                db.execute(
                    text("""UPDATE activities SET status='SUCCEEDED',
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id RETURNING *"""),
                    {"id": activity_id},
                )
                .mappings()
                .one()
            )
            from .activities import _activity_from_row

            return _activity_from_row(updated)

        plan = (
            db.execute(
                text("""SELECT content_digest FROM plans
                WHERE goal_id=:goal AND status='PUBLISHED'
                ORDER BY plan_revision DESC LIMIT 1"""),
                {"goal": goal["id"]},
            )
            .mappings()
            .first()
        )
        if plan is None:
            raise PlanRejected("缺少 PUBLISHED plan")

        profile_ids = [audit.verification_profile_id]
        audit_ids = [audit_id]
        release_content = {
            "goal_id": str(goal["id"]),
            "barrier_id": str(barrier["id"]),
            "write_epoch": barrier["write_epoch"],
            "goal_contract_digest": goal["contract_digest"],
            "plan_digest": plan["content_digest"],
            "candidate_manifest_id": str(outcome.candidate_manifest_id),
            "verification_profile_ids": sorted(str(i) for i in profile_ids),
            "audit_ids": sorted(str(i) for i in audit_ids),
            "evidence_ids": sorted(str(i) for i in outcome.evidence_ids),
        }
        release_digest = _content_digest("ReleaseManifest", release_content)
        release_id = uuid4()
        db.execute(
            text("""INSERT INTO release_manifests(
              id,project_id,goal_id,barrier_id,write_epoch,goal_contract_digest,plan_digest,
              candidate_manifest_id,verification_profile_ids,audit_ids,evidence_ids,content_digest)
            VALUES(
              :id,:project,:goal,:barrier,:epoch,:gdigest,:pdigest,
              :candidate,:profiles,:audits,:evidence,:digest)"""),
            {
                "id": release_id,
                "project": goal["project_id"],
                "goal": goal["id"],
                "barrier": barrier["id"],
                "epoch": barrier["write_epoch"],
                "gdigest": goal["contract_digest"],
                "pdigest": plan["content_digest"],
                "candidate": outcome.candidate_manifest_id,
                "profiles": profile_ids,
                "audits": audit_ids,
                "evidence": outcome.evidence_ids,
                "digest": release_digest,
            },
        )
        db.execute(
            text("""UPDATE finalization_barriers SET status='RELEASED',
              updated_at=clock_timestamp() WHERE id=:id"""),
            {"id": barrier["id"]},
        )
        from .obligations import (
            assert_attempt_obligations_assessed,
            assert_goal_ready_for_done,
            assert_no_pending_obligations,
        )

        # Goal DONE（PASS）前：本 FINALIZE attempt 义务已 ASSESSED，且候选无 pending
        assert_attempt_obligations_assessed(
            db, activity_id=activity_id, attempt_id=attempt["id"]
        )
        assert_no_pending_obligations(db, subject_id=outcome.candidate_manifest_id)
        # 未决 effect / 未确认 Stop：失败关闭（与义务门正交）
        assert_goal_ready_for_done(db, goal["id"])

        total = len([c for c in goal["contract"]["success_criteria"] if c.get("required")])
        done_goal = (
            db.execute(
                text("""UPDATE goals SET status='DONE', previous_status='VERIFYING',
                  release_manifest_id=:release, criterion_verified=:verified,
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id RETURNING *"""),
                {"id": goal["id"], "release": release_id, "verified": total},
            )
            .mappings()
            .one()
        )
        from .events import append_goal_event

        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal["id"],
            event_type="GOAL_STATE_CHANGED",
            entity_id=goal["id"],
            entity_state_revision=done_goal["state_revision"],
            resource_type="GOAL",
        )
        db.execute(
            text("""UPDATE activity_attempts SET status='COMPLETED', finished_at=:now,
              updated_at=clock_timestamp() WHERE id=:id"""),
            {"id": attempt["id"], "now": now},
        )
        db.execute(
            text("""UPDATE resource_reservations SET status='RELEASED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""),
            {"id": attempt["id"]},
        )
        db.execute(
            text("""UPDATE budget_reservations SET status='RELEASED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""),
            {"id": attempt["id"]},
        )
        updated = (
            db.execute(
                text("""UPDATE activities SET status='SUCCEEDED',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id RETURNING *"""),
                {"id": activity_id},
            )
            .mappings()
            .one()
        )
        from .activities import _activity_from_row

        return _activity_from_row(updated)


def recover_finalization(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: FinalizationRecoveryRequest,
) -> CommandOperation:
    """BLOCKED+SEALED 屏障的唯一恢复入口；不能借此强制 DONE。"""
    path = f"/api/v1/goals/{goal_id}/finalization-recovery"
    request_scope = json.dumps([str(goal_id), subject, "POST", path, key], separators=(",", ":"))
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
        goal = (
            db.execute(text("SELECT * FROM goals WHERE id=:id FOR UPDATE"), {"id": goal_id})
            .mappings()
            .first()
        )
        if goal is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, goal["project_id"], subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": goal["project_id"]},
        ).scalar_one()
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": request_scope},
        )
        old = (
            db.execute(
                text("SELECT body_digest,result FROM project_requests WHERE scope=:scope"),
                {"scope": request_scope},
            )
            .mappings()
            .first()
        )
        if old:
            if old["body_digest"] != request_digest:
                raise ProjectConflict()
            return CommandOperation.model_validate(old["result"])
        if trust != "OPEN":
            raise TrustBlocked()
        if goal["status"] != "BLOCKED":
            raise InvalidGoalState(goal["status"])
        if goal["block_reason"] not in (
            "FINALIZATION_FAIL",
            "FINALIZATION_INSUFFICIENT",
        ):
            raise InvalidGoalState(goal["block_reason"] or "BLOCKED")
        if goal["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        if body.expected_plan_revision != goal["plan_revision"]:
            raise StateRevisionConflict()
        if body.expected_write_epoch != goal["write_epoch"]:
            raise StateRevisionConflict()

        barrier = (
            db.execute(
                text("""SELECT * FROM finalization_barriers
                WHERE id=:id AND goal_id=:goal FOR UPDATE"""),
                {"id": body.barrier_id, "goal": goal_id},
            )
            .mappings()
            .first()
        )
        if barrier is None:
            raise ScopeNotFound()
        if barrier["status"] != "SEALED":
            raise InvalidGoalState(barrier["status"])
        if barrier["write_epoch"] != goal["write_epoch"]:
            raise StateRevisionConflict()

        if body.action == "REVERIFY":
            if goal["block_reason"] != "FINALIZATION_INSUFFICIENT":
                raise InvalidGoalState("FINALIZATION_FAIL")
        else:
            if goal["block_reason"] not in (
                "FINALIZATION_FAIL",
                "FINALIZATION_INSUFFICIENT",
            ):
                raise InvalidGoalState(goal["block_reason"] or "BLOCKED")

        # 未证实停止 / UNKNOWN / 隔离资源时不得开放写入或重审（AB09）。
        unknown = db.execute(
            text("""SELECT count(*) FROM effect_intents e
            JOIN activities a ON a.id=e.activity_id
            WHERE a.goal_id=:goal AND e.status IN ('DISPATCHED','UNKNOWN')"""),
            {"goal": goal_id},
        ).scalar_one()
        if int(unknown) > 0:
            raise InvalidGoalState("UNKNOWN_EFFECTS")
        from .activation_terminations import goal_has_unresolved_no_progress_stop
        from .audits import goal_has_blocking_review
        from .stops import goal_has_unconfirmed_stop_barrier

        if goal_has_unconfirmed_stop_barrier(db, goal_id):
            raise InvalidGoalState("STOP_UNCONFIRMED")
        if goal_has_blocking_review(db, goal_id):
            raise InvalidGoalState("GOAL_REVIEW_BLOCKER")
        if goal_has_unresolved_no_progress_stop(db, goal_id):
            raise InvalidGoalState("NO_PROGRESS_STOP_UNRESOLVED")
        running_verify = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND kind IN ('FINALIZE','AUDIT')
                  AND status IN ('READY','RUNNING','RECOVERING')"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        if int(running_verify) > 0:
            raise InvalidGoalState("VERIFY_IN_FLIGHT")

        activity_id = uuid4()
        if body.action == "REVERIFY":
            # 保留 SEALED 与 write_epoch；新建 FINALIZE；Goal→VERIFYING。
            prior = (
                db.execute(
                    text(
                        """SELECT binding, verification_assignments, target_id FROM activities
                        WHERE goal_id=:goal AND kind='FINALIZE' AND status='SUCCEEDED'
                        ORDER BY updated_at DESC LIMIT 1"""
                    ),
                    {"goal": goal_id},
                )
                .mappings()
                .first()
            )
            if prior is None:
                raise PlanRejected("缺少已受理 FINALIZE 以复用绑定")
            assignments = list(prior["verification_assignments"] or [])
            for item in assignments:
                item["audit_round"] = int(item.get("audit_round") or 1) + 1
            db.execute(
                text("""INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,NULL,:goal,'FINALIZE','CANDIDATE',:candidate,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'READY',1,'{}',0,
                  CAST(:resources AS jsonb))"""),
                {
                    "id": activity_id,
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "candidate": prior["target_id"],
                    "binding": json.dumps(prior["binding"]),
                    "assignments": json.dumps(assignments),
                    "resources": PLAN_RESOURCES.model_dump_json(),
                },
            )
            updated = (
                db.execute(
                    text(
                        """UPDATE goals SET status='VERIFYING', previous_status='BLOCKED',
                          block_reason=NULL, state_revision=state_revision+1,
                          updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": goal_id},
                )
                .mappings()
                .one()
            )
            write_epoch = goal["write_epoch"]
            final_status = "VERIFYING"
        else:
            # REWORK：排空后 ABORTED、新 epoch、PLANNING + PLAN。
            db.execute(
                text(
                    """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                    WHERE goal_id=:goal AND status IN ('READY','RUNNING','RECOVERING')
                      AND kind = ANY(:kinds)"""
                ),
                {
                    "goal": goal_id,
                    "kinds": ["PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE"],
                },
            )
            db.execute(
                text(
                    """UPDATE finalization_barriers SET status='ABORTED',
                      updated_at=clock_timestamp() WHERE id=:id"""
                ),
                {"id": barrier["id"]},
            )
            new_epoch = str(int(goal["write_epoch"]) + 1)
            policy_digest = db.execute(
                text("SELECT content_digest FROM policies WHERE id=:id"),
                {"id": goal["contract"]["policy_id"]},
            ).scalar_one()
            model_digest = db.execute(
                text("SELECT content_digest FROM model_profiles WHERE id=:id"),
                {"id": goal["contract"]["model_profile_id"]},
            ).scalar_one()
            skill_digest = db.execute(
                text("SELECT content_digest FROM skill_sets WHERE id=:id"),
                {"id": goal["contract"]["skill_set_id"]},
            ).scalar_one()
            binding = ExecutionBinding(
                goal_contract_revision=goal["contract_revision"],
                goal_contract_digest=goal["contract_digest"],
                task_contract_revision=None,
                task_contract_digest=None,
                plan_revision=goal["plan_revision"],
                subject_digest=goal["contract_digest"],
                policy_digest=policy_digest,
                model_profile_digest=model_digest,
                skill_set_digest=skill_digest,
            )
            db.execute(
                text(
                    """INSERT INTO activities(
                      id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                      binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                      retry_count,resources)
                    VALUES(
                      :id,:project,:goal,NULL,:goal,'PLAN','GOAL_PLAN',:goal,
                      CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,CAST(:resources AS jsonb))"""
                ),
                {
                    "id": activity_id,
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "binding": binding.model_dump_json(),
                    "resources": PLAN_RESOURCES.model_dump_json(),
                },
            )
            # claim(PLAN) 要求 PLANNING；与 replan 一致，不以 RUNNING 挂 READY PLAN。
            updated = (
                db.execute(
                    text(
                        """UPDATE goals SET status='PLANNING', previous_status='BLOCKED',
                          block_reason=NULL, write_epoch=:epoch,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": goal_id, "epoch": new_epoch},
                )
                .mappings()
                .one()
            )
            write_epoch = new_epoch
            final_status = "PLANNING"

        result = CommandResult(
            goal_id=goal_id,
            barrier_id=barrier["id"],
            action=body.action,
            write_epoch=write_epoch,
            activity_id=activity_id,
            final_status=final_status,
        )
        command_id = uuid4()
        command_row = (
            db.execute(
                text(
                    """INSERT INTO command_operations(
                      id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                    VALUES(
                      :id,:project,:goal,'RECOVER_FINALIZATION','SUCCEEDED',:digest,
                      CAST(:result AS jsonb),NULL,:subject)
                    RETURNING *"""
                ),
                {
                    "id": command_id,
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "digest": request_digest,
                    "result": result.model_dump_json(),
                    "subject": subject,
                },
            )
            .mappings()
            .one()
        )
        command = _command_from_row(command_row)
        db.execute(
            text(
                "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
            ),
            {
                "scope": request_scope,
                "digest": request_digest,
                "result": command.model_dump_json(),
            },
        )
        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            event_type="GOAL_STATE_CHANGED",
            entity_id=goal_id,
            entity_state_revision=updated["state_revision"],
            resource_type="GOAL",
        )
        return command


def get_release(engine: Engine, goal_id: UUID, project_ids: list[str]) -> ReleaseView:
    with engine.connect() as db:
        goal = (
            db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": goal_id})
            .mappings()
            .first()
        )
        if goal is None:
            raise ScopeNotFound()
        if project_ids and str(goal["project_id"]) not in project_ids:
            raise ScopeNotFound()
        if goal["release_manifest_id"] is None:
            raise ScopeNotFound()
        row = (
            db.execute(
                text("SELECT * FROM release_manifests WHERE id=:id"),
                {"id": goal["release_manifest_id"]},
            )
            .mappings()
            .one()
        )
        validity_rows = (
            db.execute(
                text(
                    """SELECT decision_ids FROM release_validity_records
                    WHERE release_manifest_id=:id
                    ORDER BY created_at, id"""
                ),
                {"id": goal["release_manifest_id"]},
            )
            .mappings()
            .all()
        )
        if validity_rows:
            merged: list[UUID] = []
            seen: set[UUID] = set()
            for vrow in validity_rows:
                for did in vrow["decision_ids"] or []:
                    if did not in seen:
                        seen.add(did)
                        merged.append(did)
            validity = ReleaseValidity(status="INVALIDATED", decision_ids=merged)
        else:
            validity = ReleaseValidity(status="VALID", decision_ids=[])
        return ReleaseView(
            manifest=_release_from_row(row),
            validity=validity,
        )
