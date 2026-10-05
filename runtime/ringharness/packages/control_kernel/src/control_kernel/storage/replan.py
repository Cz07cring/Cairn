"""Goal replan：RUNNING 下创建新 PLAN；有效最终屏障下拒绝。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.runtime import (
    CommandOperation,
    CommandResult,
    ExecutionBinding,
    InvalidGoalState,
    ReplanRequest,
    StateRevisionConflict,
)
from .activities import PLAN_RESOURCES, _activity_from_row, _command_from_row
from .events import append_goal_event
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict


def replan_goal(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ReplanRequest,
) -> CommandOperation:
    path = f"/api/v1/goals/{goal_id}/replan"
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
        # 普通 replan 仅 RUNNING；屏障/恢复走独立入口，不能借 replan 绕过。
        if goal["status"] != "RUNNING":
            raise InvalidGoalState(goal["status"])
        if goal["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        if body.expected_plan_revision != goal["plan_revision"]:
            raise StateRevisionConflict()

        barrier = db.execute(
            text(
                """SELECT 1 FROM finalization_barriers
                WHERE goal_id=:goal AND status IN ('DRAINING','SEALED','RELEASED')
                LIMIT 1"""
            ),
            {"goal": goal_id},
        ).first()
        if barrier is not None:
            raise InvalidGoalState("VERIFYING")

        max_revisions = int(goal["contract"]["retry_policy"]["max_plan_revisions"])
        if goal["plan_revision"] is not None and goal["plan_revision"] >= max_revisions:
            raise InvalidGoalState("PLAN_REVISION_EXHAUSTED")

        # 取消同 Goal 残留 READY 工程活动，避免旧图与新 PLAN 并行。
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE goal_id=:goal AND status='READY'
                  AND kind = ANY(:kinds)"""
            ),
            {
                "goal": goal_id,
                "kinds": ["PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE"],
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
        activity_id = uuid4()
        activity_row = (
            db.execute(
                text(
                    """INSERT INTO activities(
                      id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                      binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                      retry_count,resources)
                    VALUES(
                      :id,:project,:goal,NULL,:goal,'PLAN','GOAL_PLAN',:goal,
                      CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,CAST(:resources AS jsonb))
                    RETURNING *"""
                ),
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
                text(
                    """UPDATE goals SET status='PLANNING', previous_status='RUNNING',
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id RETURNING *"""
                ),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        command_id = uuid4()
        result = CommandResult(goal_id=goal_id, plan_revision=goal["plan_revision"])
        command_row = (
            db.execute(
                text(
                    """INSERT INTO command_operations(
                      id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                    VALUES(
                      :id,:project,:goal,'REPLAN','SUCCEEDED',:digest,CAST(:result AS jsonb),NULL,:subject)
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
            entity_state_revision=updated_goal["state_revision"],
            resource_type="GOAL",
        )
        _ = _activity_from_row(activity_row)
        return command
