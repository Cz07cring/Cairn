"""INTEGRATE：全部 Task DONE 后串行集成，再开启最终屏障。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..protocols.integrate import IntegrateSuccessOutcome
from ..protocols.plans import ActivityOutcomeRequest
from ..protocols.runtime import (
    ActivityResource,
    ExecutionBinding,
    LeaseRejected,
    PlanRejected,
    Resources,
    StateRevisionConflict,
    WorkerForbidden,
)
from .finalization import open_finalization_barrier
from .policies import ScopeNotFound


def maybe_schedule_integrate(db: Connection, goal_id: UUID) -> None:
    """全部 Task DONE 且 Goal RUNNING、尚无 INTEGRATE/屏障时，创建 READY INTEGRATE。"""
    goal = (
        db.execute(text("SELECT * FROM goals WHERE id=:id FOR UPDATE"), {"id": goal_id})
        .mappings()
        .first()
    )
    if goal is None or goal["status"] != "RUNNING":
        return
    # 重规划后旧图的 VERIFYING/FAIL 不属于当前图，不得永久挡住新图的最终屏障。
    unfinished = db.execute(
        text("""SELECT 1 FROM tasks
        WHERE goal_id=:goal AND plan_revision=:revision
          AND status NOT IN ('DONE','CANCELLED')
        LIMIT 1"""),
        {"goal": goal_id, "revision": goal["plan_revision"]},
    ).first()
    if unfinished is not None:
        return
    any_done = db.execute(
        text("""SELECT 1 FROM tasks
        WHERE goal_id=:goal AND plan_revision=:revision AND status='DONE' LIMIT 1"""),
        {"goal": goal_id, "revision": goal["plan_revision"]},
    ).first()
    if any_done is None:
        return
    existing = db.execute(
        text("""SELECT 1 FROM activities
        WHERE goal_id=:goal AND kind='INTEGRATE'
          AND status NOT IN ('FAILED','CANCELLED')
        LIMIT 1"""),
        {"goal": goal_id},
    ).first()
    if existing is not None:
        return
    barrier = db.execute(
        text("""SELECT 1 FROM finalization_barriers WHERE goal_id=:goal LIMIT 1"""),
        {"goal": goal_id},
    ).first()
    if barrier is not None:
        return

    # 至少需要一条 GOAL/GLOBAL 标准，否则集成后也无法 FINALIZE。
    has_global = False
    for criterion in goal["contract"]["success_criteria"]:
        if not criterion.get("required"):
            continue
        profile = (
            db.execute(
                text("SELECT config FROM verification_profiles WHERE id=:id"),
                {"id": criterion["verification_profile_id"]},
            )
            .mappings()
            .first()
        )
        if profile is None:
            continue
        cfg = profile["config"] or {}
        if cfg.get("target_scope") == "GOAL" and list(cfg.get("required_layers") or []) == [
            "GLOBAL"
        ]:
            has_global = True
            break
    if not has_global:
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
        subject_digest=goal["contract_digest"],
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
    db.execute(
        text("""INSERT INTO activities(
          id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
          binding,verification_assignments,status,state_revision,depends_on_activity_ids,
          retry_count,resources)
        VALUES(
          :id,:project,:goal,NULL,:goal,'INTEGRATE','INTEGRATION',:goal,
          CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,
          CAST(:resources AS jsonb))"""),
        {
            "id": uuid4(),
            "project": goal["project_id"],
            "goal": goal_id,
            "binding": binding.model_dump_json(),
            "resources": resources.model_dump_json(),
        },
    )


def submit_integrate_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if not isinstance(body.outcome, IntegrateSuccessOutcome):
        raise PlanRejected("INTEGRATE outcome 形状无效")
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
        if activity["kind"] != "INTEGRATE":
            raise PlanRejected("非 INTEGRATE 活动")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if activity["goal_id"] is not None:
            from .stops import assert_goal_allows_success_outcome

            assert_goal_allows_success_outcome(db, activity["goal_id"])
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
        if goal["status"] != "RUNNING":
            raise PlanRejected("Goal 不在 RUNNING，不能集成")
        candidate = (
            db.execute(
                text("""SELECT id FROM candidate_manifests
                WHERE id=:id AND goal_id=:goal"""),
                {"id": outcome.candidate_manifest_id, "goal": goal["id"]},
            )
            .mappings()
            .first()
        )
        if candidate is None:
            raise PlanRejected("集成候选不属于本 Goal")

        db.execute(
            text("""UPDATE goals SET integration_commit=:commit,
              state_revision=state_revision+1, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": goal["id"], "commit": outcome.integration_commit},
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
        open_finalization_barrier(db, goal["id"], outcome.candidate_manifest_id)
        from .activities import _activity_from_row

        return _activity_from_row(updated)
