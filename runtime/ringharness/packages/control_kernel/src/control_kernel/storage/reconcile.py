"""UNKNOWN Effect 对账：创建 RECONCILE Activity，不在命令路径直接改结果。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.effects import ReconcileSuccessOutcome, ReconciliationRequest
from ..protocols.plans import ActivityOutcomeRequest
from ..protocols.runtime import (
    ActivityResource,
    CommandOperation,
    CommandResult,
    ExecutionBinding,
    LeaseRejected,
    PlanRejected,
    StateRevisionConflict,
    WorkerForbidden,
)
from .activities import PLAN_RESOURCES, _activity_from_row, _command_from_row
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict


def reconcile_effect(
    engine: Engine,
    effect_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ReconciliationRequest,
) -> CommandOperation:
    path = f"/api/v1/effects/{effect_id}/reconcile"
    request_scope = json.dumps(
        [str(effect_id), subject, "POST", path, key], separators=(",", ":")
    )
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
        # Issue #22：admission 先于 effect 行锁
        peek = (
            db.execute(
                text("SELECT goal_id FROM effect_intents WHERE id=:id"),
                {"id": effect_id},
            )
            .mappings()
            .first()
        )
        if peek is None:
            raise ScopeNotFound()
        if peek["goal_id"] is not None:
            from .goals import acquire_goal_admission_lock

            acquire_goal_admission_lock(db, peek["goal_id"])
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": peek["goal_id"]},
            ).scalar()
            if goal_status in ("DONE", "FAILED", "CANCELLED"):
                raise PlanRejected(
                    f"GOAL_ENGINEERING_CLOSED: Goal 处于 {goal_status}，禁止新对账"
                )
        effect = (
            db.execute(
                text("SELECT * FROM effect_intents WHERE id=:id FOR UPDATE"),
                {"id": effect_id},
            )
            .mappings()
            .first()
        )
        if effect is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, effect["project_id"], subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": effect["project_id"]},
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
        if effect["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        if effect["status"] != "UNKNOWN":
            raise PlanRejected("仅 UNKNOWN 效果可对账")
        # 证据须归属本 effect
        for evidence_id in body.evidence_ids:
            row = db.execute(
                text(
                    """SELECT 1 FROM evidence_envelopes
                    WHERE id=:id AND effect_id=:effect AND project_id=:project"""
                ),
                {
                    "id": evidence_id,
                    "effect": effect_id,
                    "project": effect["project_id"],
                },
            ).first()
            if row is None:
                raise PlanRejected("对账证据无效或不属于该 effect")
        # 禁止重复未完成 RECONCILE
        open_rec = db.execute(
            text(
                """SELECT 1 FROM activities
                WHERE kind='RECONCILE' AND target_type='EFFECT' AND target_id=:effect
                  AND status IN ('READY','RUNNING','WAITING','RECOVERING')
                LIMIT 1"""
            ),
            {"effect": effect_id},
        ).first()
        if open_rec is not None:
            raise PlanRejected("已有进行中的对账活动")

        goal = None
        if effect["goal_id"] is not None:
            goal = (
                db.execute(
                    text("SELECT * FROM goals WHERE id=:id FOR SHARE"),
                    {"id": effect["goal_id"]},
                )
                .mappings()
                .one()
            )
            contract = goal["contract"]
            if isinstance(contract, str):
                contract = json.loads(contract)
            # binding 必须与 claim._live_binding(RECONCILE) 一致，否则 BINDING_STALE
            policy = db.execute(
                text("SELECT content_digest FROM policies WHERE id=:id"),
                {"id": contract["policy_id"]},
            ).scalar_one()
            model = db.execute(
                text("SELECT content_digest FROM model_profiles WHERE id=:id"),
                {"id": contract["model_profile_id"]},
            ).scalar_one()
            skill = db.execute(
                text("SELECT content_digest FROM skill_sets WHERE id=:id"),
                {"id": contract["skill_set_id"]},
            ).scalar_one()
            binding = ExecutionBinding(
                goal_contract_revision=goal["contract_revision"],
                goal_contract_digest=goal["contract_digest"],
                task_contract_revision=None,
                task_contract_digest=None,
                plan_revision=goal["plan_revision"],
                subject_digest=effect["payload_digest"],
                policy_digest=policy,
                model_profile_digest=model,
                skill_set_digest=skill,
            )
            budget_scope = goal["id"]
        else:
            policy = (
                db.execute(
                    text(
                        """SELECT content_digest FROM policies
                        WHERE project_id=:project ORDER BY version DESC, created_at DESC LIMIT 1"""
                    ),
                    {"project": effect["project_id"]},
                )
                .mappings()
                .first()
            )
            if policy is None:
                raise PlanRejected("项目尚无策略")
            binding = ExecutionBinding(
                goal_contract_revision=None,
                goal_contract_digest=None,
                task_contract_revision=None,
                task_contract_digest=None,
                plan_revision=None,
                subject_digest=effect["payload_digest"],
                policy_digest=policy["content_digest"],
                model_profile_digest=None,
                skill_set_digest=None,
            )
            budget_scope = effect["project_id"]

        activity_id = uuid4()
        # 将请求意图写入 verification_assignments 旁路字段不可；用 depends 空 + resume_state 不够。
        # 诚实做法：binding 外另存命令 result；outcome 再校验 effect_id。
        resources = PLAN_RESOURCES.model_dump()
        resources["model_slots"] = 0
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,NULL,:budget,'RECONCILE','EFFECT',:effect,
                  CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,CAST(:resources AS jsonb))"""
            ),
            {
                "id": activity_id,
                "project": effect["project_id"],
                "goal": effect["goal_id"],
                "budget": budget_scope,
                "effect": effect_id,
                "binding": binding.model_dump_json(),
                "resources": json.dumps(resources),
            },
        )
        result = CommandResult(
            effect_id=effect_id,
            activity_id=activity_id,
            observed_status=body.observed_result,
            evidence_ids=list(body.evidence_ids),
        )
        command_id = uuid4()
        command_row = (
            db.execute(
                text(
                    """INSERT INTO command_operations(
                      id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                    VALUES(
                      :id,:project,:goal,'RECONCILE_EFFECT','SUCCEEDED',:digest,
                      CAST(:result AS jsonb),NULL,:subject)
                    RETURNING *"""
                ),
                {
                    "id": command_id,
                    "project": effect["project_id"],
                    "goal": effect["goal_id"],
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
        if effect["goal_id"] is not None:
            from .events import append_goal_event

            append_goal_event(
                db,
                project_id=effect["project_id"],
                goal_id=effect["goal_id"],
                event_type="COMMAND_CHANGED",
                entity_id=command_id,
                entity_state_revision=None,
                resource_type="COMMAND",
            )
        # 外部引用写入 effect，便于 outcome 核对
        db.execute(
            text(
                """UPDATE effect_intents SET external_ref=:eref, updated_at=clock_timestamp()
                WHERE id=:id AND status='UNKNOWN'"""
            ),
            {"eref": body.external_ref, "id": effect_id},
        )
        return command


def submit_reconcile_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if not isinstance(body.outcome, ReconcileSuccessOutcome):
        raise PlanRejected("RECONCILE 需要 ReconcileSuccessOutcome")
    outcome = body.outcome
    if outcome.observed_status == "UNKNOWN":
        raise PlanRejected("对账 outcome 不能仍为 UNKNOWN")
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
        # Issue #22：admission 先于 activity/attempt 行锁
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
        if peek["kind"] != "RECONCILE":
            raise PlanRejected("活动类型不是 RECONCILE")
        if peek["goal_id"] is not None:
            from .goals import acquire_goal_admission_lock

            acquire_goal_admission_lock(db, peek["goal_id"])
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": peek["goal_id"]},
            ).scalar()
            if goal_status in ("DONE", "FAILED", "CANCELLED"):
                raise PlanRejected(
                    f"GOAL_ENGINEERING_CLOSED: Goal 处于 {goal_status}，禁止对账推进"
                )
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
        if activity["kind"] != "RECONCILE":
            raise PlanRejected("活动类型不是 RECONCILE")
        if activity["status"] != "RUNNING":
            raise PlanRejected("活动未处于 RUNNING")
        if activity["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        if body.lease.activity_id != activity_id:
            raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
        attempt = (
            db.execute(
                text(
                    """SELECT * FROM activity_attempts
                    WHERE id=:id AND activity_id=:activity FOR UPDATE"""
                ),
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
        if activity["target_id"] != outcome.effect_id:
            raise PlanRejected("outcome.effect_id 与活动目标不一致")

        effect = (
            db.execute(
                text("SELECT * FROM effect_intents WHERE id=:id FOR UPDATE"),
                {"id": outcome.effect_id},
            )
            .mappings()
            .first()
        )
        if effect is None:
            raise ScopeNotFound()
        if effect["status"] != "UNKNOWN":
            raise PlanRejected("效果已非 UNKNOWN，不能对账推进")
        for evidence_id in outcome.evidence_ids:
            ok = db.execute(
                text(
                    """SELECT 1 FROM evidence_envelopes
                    WHERE id=:id AND effect_id=:effect"""
                ),
                {"id": evidence_id, "effect": outcome.effect_id},
            ).first()
            if ok is None:
                raise PlanRejected("outcome 证据无效")

        evidence = list(effect["evidence_ids"] or []) + [
            i for i in outcome.evidence_ids if i not in (effect["evidence_ids"] or [])
        ]
        db.execute(
            text(
                """UPDATE effect_intents
                  SET status=:status, evidence_ids=:evidence,
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id AND status='UNKNOWN'"""
            ),
            {
                "id": outcome.effect_id,
                "status": outcome.observed_status,
                "evidence": evidence,
            },
        )
        from .obligations import reevaluate_quarantined_obligations_for_effect

        # Issue #23：SUPERSEDED 旧义务 + 新 audit_round OPEN（禁止同 round 复开）
        reevaluate_quarantined_obligations_for_effect(db, outcome.effect_id)
        db.execute(
            text(
                """UPDATE effect_receipts SET disposition='APPLIED'
                WHERE effect_id=:effect AND disposition='PENDING_RECONCILIATION'"""
            ),
            {"effect": outcome.effect_id},
        )
        # 原生产 Activity 仍 RECOVERING 时：SUCCEEDED 采纳证据离开 RECOVERING；
        # FAILED 等 Stop CONFIRMED 后再 READY（见 claims.reassess_recovering_activity）。
        producer_id = effect["activity_id"]
        if producer_id is not None and producer_id != activity_id:
            producer = (
                db.execute(
                    text("SELECT * FROM activities WHERE id=:id FOR UPDATE"),
                    {"id": producer_id},
                )
                .mappings()
                .first()
            )
            if producer is not None and producer["status"] == "RECOVERING":
                if outcome.observed_status == "SUCCEEDED":
                    prod_updated = (
                        db.execute(
                            text(
                                """UPDATE activities SET status='SUCCEEDED',
                                  state_revision=state_revision+1,
                                  current_attempt_id=NULL,
                                  updated_at=clock_timestamp()
                                  WHERE id=:id AND status='RECOVERING'
                                  RETURNING *"""
                            ),
                            {"id": producer_id},
                        )
                        .mappings()
                        .first()
                    )
                    if prod_updated is not None and producer["goal_id"] is not None:
                        from .events import append_goal_event

                        append_goal_event(
                            db,
                            project_id=producer["project_id"],
                            goal_id=producer["goal_id"],
                            event_type="ACTIVITY_STATE_CHANGED",
                            entity_id=producer_id,
                            entity_state_revision=prod_updated["state_revision"],
                            resource_type="ACTIVITY",
                        )
                else:
                    from .claims import reassess_recovering_activity

                    reassess_recovering_activity(db, producer_id)
        now = db.execute(text("SELECT clock_timestamp()")).scalar_one()
        db.execute(
            text(
                """UPDATE activity_attempts SET status='COMPLETED', finished_at=:now,
                  updated_at=clock_timestamp() WHERE id=:id"""
            ),
            {"id": attempt["id"], "now": now},
        )
        updated = (
            db.execute(
                text(
                    """UPDATE activities SET status='SUCCEEDED',
                      state_revision=state_revision+1, current_attempt_id=NULL,
                      updated_at=clock_timestamp()
                      WHERE id=:id RETURNING *"""
                ),
                {"id": activity_id},
            )
            .mappings()
            .one()
        )
        if activity["goal_id"] is not None:
            from .events import append_goal_event

            append_goal_event(
                db,
                project_id=activity["project_id"],
                goal_id=activity["goal_id"],
                event_type="EFFECT_CHANGED",
                entity_id=outcome.effect_id,
                entity_state_revision=None,
                resource_type="EFFECT",
            )
            append_goal_event(
                db,
                project_id=activity["project_id"],
                goal_id=activity["goal_id"],
                event_type="ACTIVITY_STATE_CHANGED",
                entity_id=activity_id,
                entity_state_revision=updated["state_revision"],
                resource_type="ACTIVITY",
            )
            from .control_commands import maybe_complete_pause_or_cancel
            from .task_commands import maybe_complete_task_cancel

            maybe_complete_pause_or_cancel(db, activity["goal_id"])
            # 关联 Task 若因未知效果阻塞取消，尝试收尾
            task_id = db.execute(
                text("SELECT task_id FROM activities WHERE id=:id"),
                {"id": effect["activity_id"]},
            ).scalar()
            if task_id is not None:
                maybe_complete_task_cancel(db, task_id)
            # 对账清除 UNKNOWN 后尝试 DRAINING→SEALED（doc/01 §9；≠DONE）
            if outcome.observed_status in ("SUCCEEDED", "FAILED"):
                from .finalization import try_seal_draining_barrier

                try_seal_draining_barrier(db, activity["goal_id"])
        return _activity_from_row(updated)
