"""Activity 查询与 Goal.start 同事务写入。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.runtime import (
    ActivityResource,
    CommandOperation,
    CommandResult,
    ControlRequest,
    ExecutionBinding,
    InvalidGoalState,
    Resources,
    StateRevisionConflict,
)
from .goals import _row_to_resource
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict

PLAN_RESOURCES = Resources(
    cpu_millicores=100,
    memory_bytes=268435456,
    disk_bytes=67108864,
    model_slots=1,
    browser_slots=0,
    exclusive_labels=[],
)


def _activity_from_row(row) -> ActivityResource:
    data = dict(row)
    return ActivityResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "task_id": data["task_id"],
            "binding": data["binding"],
            "budget_scope_id": data["budget_scope_id"],
            "kind": data["kind"],
            "target": {"type": data["target_type"], "id": data["target_id"]},
            "verification_assignments": data["verification_assignments"] or [],
            "status": data["status"],
            "state_revision": data["state_revision"],
            "depends_on_activity_ids": list(data["depends_on_activity_ids"] or []),
            "wait_reason": data["wait_reason"],
            "wake_at": data["wake_at"],
            "wait_deadline_at": data["wait_deadline_at"],
            "resume_state": data["resume_state"],
            "retry_count": data["retry_count"],
            "current_attempt_id": data["current_attempt_id"],
            "resources": data["resources"],
        }
    )


def _command_from_row(row) -> CommandOperation:
    data = dict(row)
    return CommandOperation.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "kind": data["kind"],
            "status": data["status"],
            "request_digest": data["request_digest"],
            "result": data["result"],
            "error": data["error"],
        }
    )


class Activities:
    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self, activity_id: UUID, subject: str, project_ids: list[str]) -> ActivityResource:
        with self.engine.connect() as db:
            row = (
                db.execute(text("SELECT * FROM activities WHERE id=:id"), {"id": activity_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            return _activity_from_row(row)

    def get_for_worker(self, activity_id: UUID, subject: str) -> ActivityResource:
        """已登记 ACTIVE worker 且对该活动持有 ACTIVE attempt 时可读快照。

        供 TEMPORAL RunActivation（claimPlan=GET activity）使用；不依赖 JWT project_ids，
        禁止无租约扫库。
        """
        from ..protocols.runtime import WorkerForbidden

        with self.engine.connect() as db:
            worker = (
                db.execute(
                    text(
                        "SELECT id FROM workers WHERE subject=:subject AND status='ACTIVE'"
                    ),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker is None:
                raise WorkerForbidden()
            row = (
                db.execute(text("SELECT * FROM activities WHERE id=:id"), {"id": activity_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            held = db.execute(
                text(
                    """SELECT 1 FROM activity_attempts
                    WHERE activity_id=:aid AND worker_id=:wid AND status='ACTIVE'
                    LIMIT 1"""
                ),
                {"aid": activity_id, "wid": worker["id"]},
            ).first()
            if held is None:
                raise ScopeNotFound()
            return _activity_from_row(row)

    def list_for_goal(
        self,
        goal_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        after: UUID | None,
        kind: str | None,
        status: str | None,
    ) -> list[ActivityResource]:
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
                text("""SELECT * FROM activities WHERE project_id=:project AND goal_id=:goal
                AND (CAST(:kind AS text) IS NULL OR kind=CAST(:kind AS text))
                AND (CAST(:status AS text) IS NULL OR status=CAST(:status AS text))
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                  SELECT created_at,id FROM activities WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""),
                {
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "kind": kind,
                    "status": status,
                    "after": after,
                    "limit": limit,
                },
            ).mappings()
            return [_activity_from_row(row) for row in rows]

    def list_for_task(
        self,
        task_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        after: UUID | None,
        kind: str | None,
    ) -> list[ActivityResource]:
        with self.engine.connect() as db:
            task = (
                db.execute(
                    text("SELECT project_id FROM tasks WHERE id=:id"), {"id": task_id}
                )
                .mappings()
                .first()
            )
            if task is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, task["project_id"], subject, project_ids)
            rows = db.execute(
                text(
                    """SELECT * FROM activities WHERE project_id=:project AND task_id=:task
                AND (CAST(:kind AS text) IS NULL OR kind=CAST(:kind AS text))
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                  SELECT created_at,id FROM activities WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""
                ),
                {
                    "project": task["project_id"],
                    "task": task_id,
                    "kind": kind,
                    "after": after,
                    "limit": limit,
                },
            ).mappings()
            return [_activity_from_row(row) for row in rows]


class Commands:
    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self, command_id: UUID, subject: str, project_ids: list[str]) -> CommandOperation:
        with self.engine.connect() as db:
            row = (
                db.execute(
                    text("SELECT * FROM command_operations WHERE id=:id"), {"id": command_id}
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            if row["project_id"] is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            return _command_from_row(row)

    def list_commands(
        self,
        subject: str,
        project_ids: list[str],
        *,
        project_id: UUID | None,
        goal_id: UUID | None,
        limit: int,
        after: UUID | None,
    ) -> list[CommandOperation]:
        with self.engine.connect() as db:
            if project_id is not None:
                ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
                scope_filter = "project_id=:project"
                params: dict = {
                    "project": project_id,
                    "limit": limit,
                    "after": after,
                }
            else:
                # 无 project：只返本主体发起且仍可见项目下的命令。
                scope_filter = """subject=:subject AND subject<>''
                AND (
                  EXISTS (
                    SELECT 1 FROM project_memberships m
                    WHERE m.project_id=command_operations.project_id AND m.subject=:subject)
                  OR project_id = ANY(CAST(:ids AS uuid[]))
                )"""
                params = {
                    "subject": subject,
                    "ids": [str(i) for i in project_ids],
                    "limit": limit,
                    "after": after,
                }
            if goal_id is not None:
                scope_filter += " AND goal_id=:goal"
                params["goal"] = goal_id
            rows = db.execute(
                text(
                    f"""SELECT * FROM command_operations WHERE {scope_filter}
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                  SELECT created_at,id FROM command_operations WHERE id=CAST(:after AS uuid)))
                ORDER BY created_at,id LIMIT :limit"""
                ),
                params,
            ).mappings()
            return [_command_from_row(row) for row in rows]


def start_goal(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ControlRequest,
) -> CommandOperation:
    """DRAFT→PLANNING，同事务创建 READY 的 PLAN Activity。

    LEGACY：CommandOperation SUCCEEDED。
    TEMPORAL：CommandOperation ACCEPTED（仅受理），并写入 binding + PENDING ENSURE_WORKFLOW；
    编排 ACK 后由 Relay 升为 SUCCEEDED，不在 START 时谎称已跑通。
    """
    path = f"/api/v1/goals/{goal_id}/start"
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
        if goal["status"] != "DRAFT":
            raise InvalidGoalState(goal["status"])
        if goal["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        # LEGACY 退役闸门：强制 TEMPORAL 时禁止再启动旧派发 Goal
        from control_kernel.storage.goals import (
            LegacyOrchestrationForbidden,
            temporal_orchestration_enforced,
        )

        backend_early = goal.get("orchestration_backend") or "LEGACY"
        if temporal_orchestration_enforced() and backend_early == "LEGACY":
            raise LegacyOrchestrationForbidden(
                "已配置 RING_ORCHESTRATION_REQUIRE_TEMPORAL 与 RING_TEMPORAL_TARGET，"
                "禁止再 START LEGACY Goal（仅供存量排空）"
            )
        # Goal budget scope 锁：与后续工程 dispatch 共用同一 scope 身份。
        scope_lock = json.dumps(
            ["budget", str(goal["project_id"]), str(goal_id)], separators=(",", ":")
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": scope_lock},
        )
        from .budget_clock import init_budget_clock_on_start

        # Issue #20：起计锚点在 START，DRAFT 停留不计入墙钟
        init_budget_clock_on_start(db, goal_id)
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
            plan_revision=None,
            subject_digest=goal["contract_digest"],
            policy_digest=policy_digest,
            model_profile_digest=model_digest,
            skill_set_digest=skill_digest,
        )
        activity_id = uuid4()
        activity_row = (
            db.execute(
                text("""INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,NULL,:goal,'PLAN','GOAL_PLAN',:goal,
                  CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,CAST(:resources AS jsonb))
                RETURNING *"""),
                {
                    "id": activity_id,
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "binding": binding.model_dump_json(),
                    "resources": PLAN_RESOURCES.model_dump_json(),
                },
            )
            .mappings()
            .one()
        )
        updated_goal = (
            db.execute(
                text("""UPDATE goals SET status='PLANNING', previous_status='DRAFT',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id RETURNING *"""),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        command_id = uuid4()
        result = CommandResult(goal_id=goal_id, final_status="PLANNING")
        # TEMPORAL：命令仅 ACCEPTED（受理），编排跑通由 Relay ACK 后再 SUCCEEDED；不谎称已编排。
        # LEGACY：同事务内调度完成，保持 SUCCEEDED。
        backend = goal.get("orchestration_backend") or "LEGACY"
        command_status = "ACCEPTED" if backend == "TEMPORAL" else "SUCCEEDED"
        command_row = (
            db.execute(
                text("""INSERT INTO command_operations(
                  id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                VALUES(
                  :id,:project,:goal,'START',:status,:digest,CAST(:result AS jsonb),NULL,:subject)
                RETURNING *"""),
                {
                    "id": command_id,
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "status": command_status,
                    "digest": request_digest,
                    "result": result.model_dump_json(),
                    "subject": subject,
                },
            )
            .mappings()
            .one()
        )
        if backend == "TEMPORAL":
            from .orchestration import enqueue_ensure_workflow, upsert_binding_for_temporal_start

            binding_row = upsert_binding_for_temporal_start(db, goal, command_id)
            enqueue_ensure_workflow(
                db,
                project_id=goal["project_id"],
                goal_id=goal_id,
                command_id=command_id,
                workflow_id=binding_row["workflow_id"],
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
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'GOAL_STARTED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": goal["project_id"],
                "payload": json.dumps(
                    {
                        "goal": _row_to_resource(updated_goal).model_dump(mode="json"),
                        "activity": _activity_from_row(activity_row).model_dump(mode="json"),
                        "command": command.model_dump(mode="json"),
                    }
                ),
            },
        )
        from .events import append_goal_event

        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            event_type="GOAL_STATE_CHANGED",
            entity_id=goal_id,
            entity_state_revision=updated_goal["state_revision"],
            resource_type="GOAL",
        )
        return command
