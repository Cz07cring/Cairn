"""PLAN outcome：校验并发布 Plan，创建 Task 与根 EXECUTE Activity。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from evidence_ledger.content import encode
from sqlalchemy import Engine, text

from ..domain.plan_static_validator import (
    PlanStaticViolation,
    validate_plan_static,
)
from ..protocols.goals import Budget
from ..protocols.plans import (
    ActivityOutcomeRequest,
    PlanCreate,
    PlanResource,
    TaskResource,
)
from ..protocols.runtime import (
    ActivityResource,
    ExecutionBinding,
    LeaseRejected,
    PlanRejected,
    StateRevisionConflict,
    WorkerForbidden,
)
from .activities import _activity_from_row
from .goals import _row_to_resource
from .plan_inputs import assert_attempt_plan_input
from .policies import ScopeNotFound


def _task_digest(contract: dict) -> str:
    canonical = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": "TaskContract",
                "content": contract,
                "reference_bindings": [],
            }
        )
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _plan_digest(goal_id: UUID, goal_revision: int, plan_revision: int, body: PlanCreate) -> str:
    content = {
        "goal_id": str(goal_id),
        "goal_contract_revision": goal_revision,
        "plan_revision": plan_revision,
        "reason": body.reason,
        "tasks": [
            {
                "id": str(t.id),
                "contract": t.contract.model_dump(mode="json"),
                "replaces_task_id": str(t.replaces_task_id) if t.replaces_task_id else None,
            }
            for t in sorted(body.tasks, key=lambda item: str(item.id))
        ],
        "coverage": [c.model_dump(mode="json") for c in body.coverage],
    }
    canonical = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": "Plan",
                "content": content,
                "reference_bindings": [],
            }
        )
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _goal_level_criterion_ids(db, goal) -> set[str]:
    """目标是 GOAL 层（GLOBAL 验收层）的标准 id 集合。

    **为什么需要排除**（2026-09-17 实测）：两条规则同时成立时互相死锁 ——
      ① `maybe_schedule_integrate` 要求契约里存在 required 的 GOAL/GLOBAL 标准，
         否则不开最终屏障 ⇒ Goal 永到不了 INTEGRATE/FINALIZE/DONE；
      ② 本函数的 coverage 校验要求 plan 覆盖**全部** required 标准。
    于是任何「满足开屏障条件」的契约都必然发布不了 plan：实测加了 GOAL/GLOBAL 准则后
    PLAN 回执 422「必要目标标准未被覆盖」，Goal 卡死在 PLANNING。

    语义上 GOAL 级标准本就不该由 task plan 覆盖：它们由终验（FINALIZE）在
    `_global_assignments_for_goal` 里转成 GLOBAL 验收分配。
    见 plans.get_for_worker 注释：「禁止仅用 Goal GLOBAL success_criteria 冒充任务验收」。
    故校验收敛为**只要求任务级标准被覆盖**；GOAL 级标准仍允许被显式引用
    （`goal_criterion_ids` 不排除，覆盖引用的合法性校验照旧）。
    """
    ids: set[str] = set()
    for criterion in goal["contract"]["success_criteria"]:
        profile = (
            db.execute(
                text("SELECT config FROM verification_profiles WHERE id=:id"),
                {"id": criterion["verification_profile_id"]},
            )
            .mappings()
            .first()
        )
        cfg = (profile["config"] if profile else {}) or {}
        if cfg.get("target_scope") == "GOAL":
            ids.add(criterion["id"])
    return ids


def _validate_against_goal(db, goal, body: PlanCreate) -> None:
    if body.expected_plan_revision != goal["plan_revision"]:
        raise StateRevisionConflict()
    # M4 PlanStaticValidator：结构闸门（环/悬空/producer/路径/criterion/预算）
    goal_criteria = {c["id"]: c for c in goal["contract"]["success_criteria"]}
    goal_level = _goal_level_criterion_ids(db, goal)
    required = {
        c["id"]
        for c in goal["contract"]["success_criteria"]
        if c.get("required") and c["id"] not in goal_level
    }
    try:
        validate_plan_static(
            body,
            goal_budget=Budget.model_validate(goal["contract"]["budget"]),
            goal_criterion_ids=set(goal_criteria.keys()),
            required_goal_criterion_ids=required,
        )
    except PlanStaticViolation as exc:
        raise PlanRejected(f"{exc.code}:{exc.message}") from exc
    covered = {entry.goal_criterion_id for entry in body.coverage}
    if not required.issubset(covered):
        raise PlanRejected("必要目标标准未被覆盖")
    tasks = {t.id: t for t in body.tasks}
    for entry in body.coverage:
        if entry.goal_criterion_id not in goal_criteria:
            raise PlanRejected("覆盖引用了未知目标标准")
        task = tasks[entry.task_id]
        acceptance = {c.id: c for c in task.contract.acceptance}
        if entry.task_acceptance_id not in acceptance:
            raise PlanRejected("覆盖引用了未知验收项")
        acc = acceptance[entry.task_acceptance_id]
        if acc.verification_profile_id != entry.verification_profile_id:
            raise PlanRejected("覆盖验证配置与任务验收不一致")
        if entry.goal_criterion_id not in task.contract.covers_goal_criterion_ids:
            raise PlanRejected("任务未声明覆盖该目标标准")
        profile = db.execute(
            text(
                """SELECT 1 FROM verification_profiles
                    WHERE project_id=:project AND id=:id"""
            ),
            {"project": goal["project_id"], "id": entry.verification_profile_id},
        ).first()
        if profile is None:
            raise PlanRejected("验证配置不存在")


def _plan_from_row(row) -> PlanResource:
    data = dict(row)
    return PlanResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "plan_revision": data["plan_revision"],
            "status": data["status"],
            "reason": data["reason"],
            "tasks": data["tasks"],
            "coverage": data["coverage"],
            "content_digest": data["content_digest"],
            "source_plan_input_id": data.get("source_plan_input_id"),
            "source_plan_input_digest": data.get("source_plan_input_digest"),
        }
    )


def _task_from_row(row) -> TaskResource:
    data = dict(row)
    return TaskResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "contract": data["contract"],
            "contract_digest": data["contract_digest"],
            "contract_revision": data["contract_revision"],
            "plan_revision": data["plan_revision"],
            "state_revision": data["state_revision"],
            "status": data["status"],
            "block_reason": data["block_reason"],
            "resume_state": data["resume_state"],
            "work_lineage_id": data["work_lineage_id"],
            "execution_round": data["execution_round"],
            "latest_checkpoint_id": data["latest_checkpoint_id"],
            "replaces_task_id": data["replaces_task_id"],
        }
    )


def submit_plan_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if body.lease.activity_id != activity_id:
        raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
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
        if activity["kind"] != "PLAN":
            raise PlanRejected("当前仅支持 PLAN outcome")
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
        from .stops import assert_goal_allows_success_outcome

        assert_goal_allows_success_outcome(db, goal["id"])
        if goal["status"] != "PLANNING":
            raise PlanRejected("仅 PLANNING 可发布首图")
        selected_input = assert_attempt_plan_input(db, activity, attempt)
        if selected_input is not None:
            candidate = (
                db.execute(
                    text("SELECT reason,tasks,coverage FROM plans WHERE id=:id"),
                    {"id": selected_input.candidate_plan_id},
                )
                .mappings()
                .one()
            )
            candidate_body = PlanCreate.model_validate(
                {
                    "expected_plan_revision": selected_input.expected_plan_revision,
                    "reason": candidate["reason"],
                    "tasks": candidate["tasks"],
                    "coverage": candidate["coverage"],
                }
            )
            if candidate_body.model_dump(mode="json") != body.outcome.plan.model_dump(mode="json"):
                raise PlanRejected("PLAN_INPUT_OUTCOME_MISMATCH: outcome 与固定候选正文不一致")
            bundle = (
                db.execute(
                    text("""SELECT content FROM context_bundles
                  WHERE attempt_id=:attempt AND content_digest=:digest"""),
                    {"attempt": attempt["id"], "digest": attempt["context_digest"]},
                )
                .mappings()
                .first()
            )
            if bundle is None:
                raise PlanRejected("PLAN_INPUT_CONTEXT_MISSING: attempt 未绑定固定 ContextBundle")
            assert_attempt_plan_input(db, activity, attempt, content=bundle["content"])
        scope_lock = json.dumps(
            ["budget", str(goal["project_id"]), str(goal["id"])], separators=(",", ":")
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": scope_lock},
        )
        plan_body = body.outcome.plan
        _validate_against_goal(db, goal, plan_body)
        plan_revision = 1 if goal["plan_revision"] is None else goal["plan_revision"] + 1
        digest = _plan_digest(goal["id"], goal["contract_revision"], plan_revision, plan_body)
        plan_id = uuid4()
        plan_row = (
            db.execute(
                text("""INSERT INTO plans(
                  id,project_id,goal_id,plan_revision,status,reason,tasks,coverage,content_digest,
                  source_plan_input_id,source_plan_input_digest)
                VALUES(
                  :id,:project,:goal,:revision,'PUBLISHED',:reason,
                  CAST(:tasks AS jsonb),CAST(:coverage AS jsonb),:digest,:source_id,:source_digest)
                RETURNING *"""),
                {
                    "id": plan_id,
                    "project": goal["project_id"],
                    "goal": goal["id"],
                    "revision": plan_revision,
                    "reason": plan_body.reason,
                    "tasks": json.dumps([t.model_dump(mode="json") for t in plan_body.tasks]),
                    "coverage": json.dumps([c.model_dump(mode="json") for c in plan_body.coverage]),
                    "digest": digest,
                    "source_id": selected_input.id if selected_input else None,
                    "source_digest": selected_input.content_digest if selected_input else None,
                },
            )
            .mappings()
            .one()
        )
        for entry in plan_body.coverage:
            db.execute(
                text("""INSERT INTO criterion_coverage(
                  project_id,goal_id,plan_revision,goal_criterion_id,task_id,
                  task_acceptance_id,verification_profile_id)
                VALUES(:project,:goal,:revision,:criterion,:task,:acceptance,:profile)"""),
                {
                    "project": goal["project_id"],
                    "goal": goal["id"],
                    "revision": plan_revision,
                    "criterion": entry.goal_criterion_id,
                    "task": entry.task_id,
                    "acceptance": entry.task_acceptance_id,
                    "profile": entry.verification_profile_id,
                },
            )
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
        for node in plan_body.tasks:
            contract = node.contract.model_dump(mode="json")
            contract_digest = _task_digest(contract)
            status = "READY" if not node.contract.depends_on else "PENDING"
            db.execute(
                text("""INSERT INTO tasks(
                  id,project_id,goal_id,contract,contract_digest,contract_revision,plan_revision,
                  state_revision,status,work_lineage_id,execution_round,replaces_task_id)
                VALUES(
                  :id,:project,:goal,CAST(:contract AS jsonb),:digest,1,:plan_revision,
                  1,:status,:lineage,1,:replaces)"""),
                {
                    "id": node.id,
                    "project": goal["project_id"],
                    "goal": goal["id"],
                    "contract": json.dumps(contract),
                    "digest": contract_digest,
                    "plan_revision": plan_revision,
                    "status": status,
                    "lineage": node.id,
                    "replaces": node.replaces_task_id,
                },
            )
            for dep in node.contract.depends_on:
                db.execute(
                    text("""INSERT INTO task_edges(
                      project_id,goal_id,plan_revision,from_task_id,to_task_id)
                    VALUES(:project,:goal,:revision,:src,:dst)"""),
                    {
                        "project": goal["project_id"],
                        "goal": goal["id"],
                        "revision": plan_revision,
                        "src": dep,
                        "dst": node.id,
                    },
                )
            if status == "READY":
                binding = ExecutionBinding(
                    goal_contract_revision=goal["contract_revision"],
                    goal_contract_digest=goal["contract_digest"],
                    task_contract_revision=1,
                    task_contract_digest=contract_digest,
                    plan_revision=plan_revision,
                    subject_digest=contract_digest,
                    policy_digest=policy_digest,
                    model_profile_digest=model_digest,
                    skill_set_digest=skill_digest,
                )
                db.execute(
                    text("""INSERT INTO activities(
                      id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                      binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                      retry_count,resources)
                    VALUES(
                      :id,:project,:goal,:task,:goal,'EXECUTE','TASK_WORK',:task,
                      CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,CAST(:resources AS jsonb))"""),
                    {
                        "id": uuid4(),
                        "project": goal["project_id"],
                        "goal": goal["id"],
                        "task": node.id,
                        "binding": binding.model_dump_json(),
                        "resources": node.contract.resources.model_dump_json(),
                    },
                )

        db.execute(
            text("""UPDATE goals SET status='RUNNING', previous_status='PLANNING',
              plan_revision=:revision, state_revision=state_revision+1,
              updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": goal["id"], "revision": plan_revision},
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
        result = _activity_from_row(updated)
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'PLAN_PUBLISHED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": goal["project_id"],
                "payload": json.dumps(
                    {
                        "goal": _row_to_resource(
                            db.execute(
                                text("SELECT * FROM goals WHERE id=:id"),
                                {"id": goal["id"]},
                            )
                            .mappings()
                            .one()
                        ).model_dump(mode="json"),
                        "plan": _plan_from_row(plan_row).model_dump(mode="json"),
                        "activity": result.model_dump(mode="json"),
                    }
                ),
            },
        )
        from .events import append_goal_event

        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal["id"],
            event_type="PLAN_PUBLISHED",
            entity_id=plan_row["id"],
            entity_state_revision=None,
            resource_type="PLAN",
        )
        # 确认本 activation 编译进 ContextBundle 的反馈已消费（去重插入）。
        if attempt["context_digest"] is not None:
            bundle = (
                db.execute(
                    text(
                        """SELECT content FROM context_bundles
                        WHERE attempt_id=:attempt AND content_digest=:digest"""
                    ),
                    {"attempt": attempt["id"], "digest": attempt["context_digest"]},
                )
                .mappings()
                .first()
            )
            if bundle is not None:
                from uuid import UUID as _UUID

                from .feedback import record_feedback_consumptions

                ids = [
                    _UUID(str(i))
                    for i in (bundle["content"] or {}).get("planning_feedback_ids") or []
                ]
                record_feedback_consumptions(
                    db,
                    plan_activity_id=activity_id,
                    attempt_id=attempt["id"],
                    feedback_ids=ids,
                )
        return result


class Plans:
    def __init__(self, engine: Engine):
        self.engine = engine

    def submit_candidate(
        self,
        goal_id: UUID,
        subject: str,
        project_ids: list[str],
        key: str,
        body: PlanCreate,
    ) -> PlanResource:
        """公共 POST：只存 CANDIDATE，不分配 plan_revision、不发布 Task。"""
        path = f"/api/v1/goals/{goal_id}/plans"
        request_scope = json.dumps(
            [str(goal_id), subject, "POST", path, key], separators=(",", ":")
        )
        request_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        with self.engine.begin() as db:
            from ..protocols.runtime import InvalidGoalState
            from .policies import ConfigurationVersions, TrustBlocked
            from .projects import ProjectConflict

            goal = (
                db.execute(
                    text("SELECT * FROM goals WHERE id=:id FOR UPDATE"),
                    {"id": goal_id},
                )
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
                return PlanResource.model_validate(old["result"])
            if trust != "OPEN":
                raise TrustBlocked()
            if goal["status"] not in ("PLANNING", "RUNNING"):
                raise InvalidGoalState(goal["status"])
            # 有效屏障时拒绝新候选（最终化中不得改图）。
            barrier = db.execute(
                text(
                    """SELECT 1 FROM finalization_barriers
                    WHERE goal_id=:goal AND status IN ('DRAINING','SEALED') LIMIT 1"""
                ),
                {"goal": goal_id},
            ).first()
            if barrier is not None:
                raise PlanRejected("存在有效最终屏障，不能提交候选计划")
            _validate_against_goal(db, goal, body)
            # 候选不是已发布 Plan：摘要不走 Content v3 Plan（要求 plan_revision≥1）。
            payload = {
                "goal_id": str(goal["id"]),
                "goal_contract_revision": goal["contract_revision"],
                "plan": body.model_dump(mode="json"),
            }
            digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            plan_id = uuid4()
            row = (
                db.execute(
                    text(
                        """INSERT INTO plans(
                          id,project_id,goal_id,plan_revision,status,reason,tasks,coverage,content_digest)
                        VALUES(
                          :id,:project,:goal,NULL,'CANDIDATE',:reason,
                          CAST(:tasks AS jsonb),CAST(:coverage AS jsonb),:digest)
                        RETURNING *"""
                    ),
                    {
                        "id": plan_id,
                        "project": goal["project_id"],
                        "goal": goal_id,
                        "reason": body.reason,
                        "tasks": json.dumps([t.model_dump(mode="json") for t in body.tasks]),
                        "coverage": json.dumps([c.model_dump(mode="json") for c in body.coverage]),
                        "digest": digest,
                    },
                )
                .mappings()
                .one()
            )
            result = _plan_from_row(row)
            db.execute(
                text(
                    "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
                ),
                {
                    "scope": request_scope,
                    "digest": request_digest,
                    "result": result.model_dump_json(),
                },
            )
            from .events import append_goal_event

            append_goal_event(
                db,
                project_id=goal["project_id"],
                goal_id=goal_id,
                event_type="PLAN_CANDIDATE_CREATED",
                entity_id=plan_id,
                entity_state_revision=None,
                resource_type="PLAN",
            )
            return result

    def list_for_goal(
        self,
        goal_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        after: UUID | None = None,
    ) -> list[PlanResource]:
        from .policies import ConfigurationVersions

        with self.engine.connect() as db:
            goal = (
                db.execute(text("SELECT project_id FROM goals WHERE id=:id"), {"id": goal_id})
                .mappings()
                .first()
            )
            if goal is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, goal["project_id"], subject, project_ids)
            rows = db.execute(
                text("""SELECT * FROM plans WHERE project_id=:project AND goal_id=:goal
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                  SELECT created_at,id FROM plans WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""),
                {
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "after": after,
                    "limit": limit,
                },
            ).mappings()
            return [_plan_from_row(row) for row in rows]


class Tasks:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list_for_goal(
        self,
        goal_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        status: str | None = None,
        after: UUID | None = None,
    ) -> list[TaskResource]:
        from .policies import ConfigurationVersions

        with self.engine.connect() as db:
            goal = (
                db.execute(text("SELECT project_id FROM goals WHERE id=:id"), {"id": goal_id})
                .mappings()
                .first()
            )
            if goal is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, goal["project_id"], subject, project_ids)
            rows = db.execute(
                text("""SELECT * FROM tasks WHERE project_id=:project AND goal_id=:goal
                AND (CAST(:status AS text) IS NULL OR status=CAST(:status AS text))
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                  SELECT created_at,id FROM tasks WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""),
                {
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "status": status,
                    "after": after,
                    "limit": limit,
                },
            ).mappings()
            return [_task_from_row(row) for row in rows]

    def get(self, task_id: UUID, subject: str, project_ids: list[str]) -> TaskResource:
        from .policies import ConfigurationVersions

        with self.engine.connect() as db:
            row = (
                db.execute(text("SELECT * FROM tasks WHERE id=:id"), {"id": task_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            return _task_from_row(row)

    def get_for_worker(self, task_id: UUID, subject: str) -> TaskResource:
        """已登记 ACTIVE worker 且对该 Task 下某活动持有 ACTIVE attempt 时可读合同。

        供 EXECUTE seal：取 acceptance 上的 VerificationProfile（MECHANICAL），
        禁止仅用 Goal GLOBAL success_criteria 冒充任务验收。
        """
        with self.engine.connect() as db:
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
            row = (
                db.execute(text("SELECT * FROM tasks WHERE id=:id"), {"id": task_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            held = db.execute(
                text(
                    """SELECT 1 FROM activity_attempts aa
                    INNER JOIN activities a ON a.id = aa.activity_id
                    WHERE a.task_id=:tid AND aa.worker_id=:wid AND aa.status='ACTIVE'
                    LIMIT 1"""
                ),
                {"tid": task_id, "wid": worker["id"]},
            ).first()
            if held is None:
                raise ScopeNotFound()
            return _task_from_row(row)
