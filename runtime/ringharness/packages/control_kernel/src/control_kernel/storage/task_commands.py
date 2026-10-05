"""Task cancel / retry：取消收尾与 replacement 保留 lineage。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..protocols.runtime import (
    CommandOperation,
    CommandResult,
    ControlRequest,
    ExecutionBinding,
    InvalidGoalState,
    Resources,
    StateRevisionConflict,
)
from .activities import _command_from_row
from .events import append_goal_event
from .plans import _task_digest, _task_from_row
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict

_TASK_KINDS = ("EXECUTE", "AUDIT", "INTEGRATE")


class InvalidTaskState(Exception):
    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


def _count_running_for_task(db: Connection, task_id: UUID) -> int:
    return int(
        db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE task_id=:task AND status='RUNNING' AND kind = ANY(:kinds)"""
            ),
            {"task": task_id, "kinds": list(_TASK_KINDS)},
        ).scalar_one()
    )


def _count_unknown_for_task(db: Connection, task_id: UUID) -> int:
    return int(
        db.execute(
            text(
                """SELECT count(*) FROM effect_intents e
                JOIN activities a ON a.id=e.activity_id
                WHERE a.task_id=:task AND e.status IN ('DISPATCHED','UNKNOWN')"""
            ),
            {"task": task_id},
        ).scalar_one()
    )


def _cancel_ready_for_task(db: Connection, task_id: UUID) -> None:
    db.execute(
        text(
            """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
            WHERE task_id=:task AND status='READY' AND kind = ANY(:kinds)"""
        ),
        {"task": task_id, "kinds": list(_TASK_KINDS)},
    )


def maybe_complete_task_cancel(db: Connection, task_id: UUID) -> None:
    task = (
        db.execute(text("SELECT * FROM tasks WHERE id=:id FOR UPDATE"), {"id": task_id})
        .mappings()
        .first()
    )
    if task is None:
        return
    if task["status"] != "BLOCKED" or task["block_reason"] != "CANCELLING":
        return
    if _count_running_for_task(db, task_id) > 0 or _count_unknown_for_task(db, task_id) > 0:
        return
    _cancel_ready_for_task(db, task_id)
    db.execute(
        text(
            """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
            WHERE task_id=:task AND status IN ('WAITING','RECOVERING','PENDING')
              AND kind = ANY(:kinds)"""
        ),
        {"task": task_id, "kinds": list(_TASK_KINDS)},
    )
    updated = (
        db.execute(
            text(
                """UPDATE tasks SET status='CANCELLED', block_reason=NULL, resume_state=NULL,
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id RETURNING *"""
            ),
            {"id": task_id},
        )
        .mappings()
        .one()
    )
    append_goal_event(
        db,
        project_id=task["project_id"],
        goal_id=task["goal_id"],
        event_type="TASK_STATE_CHANGED",
        entity_id=task_id,
        entity_state_revision=updated["state_revision"],
        resource_type="TASK",
    )


def _command_write(
    db: Connection,
    *,
    project_id: UUID,
    goal_id: UUID,
    kind: str,
    request_scope: str,
    request_digest: str,
    result: CommandResult,
    subject: str,
) -> CommandOperation:
    command_id = uuid4()
    command_row = (
        db.execute(
            text(
                """INSERT INTO command_operations(
                  id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                VALUES(
                  :id,:project,:goal,:kind,'SUCCEEDED',:digest,CAST(:result AS jsonb),NULL,:subject)
                RETURNING *"""
            ),
            {
                "id": command_id,
                "project": project_id,
                "goal": goal_id,
                "kind": kind,
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
    return command


def cancel_task(
    engine: Engine,
    task_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ControlRequest,
) -> CommandOperation:
    path = f"/api/v1/tasks/{task_id}/cancel"
    request_scope = json.dumps([str(task_id), subject, "POST", path, key], separators=(",", ":"))
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
        task = (
            db.execute(text("SELECT * FROM tasks WHERE id=:id FOR UPDATE"), {"id": task_id})
            .mappings()
            .first()
        )
        if task is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, task["project_id"], subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": task["project_id"]},
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
        goal = (
            db.execute(
                text("SELECT * FROM goals WHERE id=:id FOR SHARE"),
                {"id": task["goal_id"]},
            )
            .mappings()
            .one()
        )
        if goal["status"] in ("DONE", "CANCELLED", "FAILED", "DRAFT"):
            raise InvalidGoalState(goal["status"])
        if task["status"] in ("DONE", "CANCELLED", "FAILED"):
            raise InvalidTaskState(task["status"])
        if task["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()

        _cancel_ready_for_task(db, task_id)
        blocked = (
            _count_running_for_task(db, task_id) > 0 or _count_unknown_for_task(db, task_id) > 0
        )
        if blocked:
            updated = (
                db.execute(
                    text(
                        """UPDATE tasks SET status='BLOCKED', block_reason='CANCELLING',
                          resume_state=:resume, state_revision=state_revision+1,
                          updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": task_id, "resume": task["status"]},
                )
                .mappings()
                .one()
            )
            final = "CANCELLING"
        else:
            db.execute(
                text(
                    """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                    WHERE task_id=:task AND status IN ('WAITING','RECOVERING','PENDING')
                      AND kind = ANY(:kinds)"""
                ),
                {"task": task_id, "kinds": list(_TASK_KINDS)},
            )
            updated = (
                db.execute(
                    text(
                        """UPDATE tasks SET status='CANCELLED', block_reason=NULL, resume_state=NULL,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": task_id},
                )
                .mappings()
                .one()
            )
            final = "CANCELLED"
        append_goal_event(
            db,
            project_id=task["project_id"],
            goal_id=task["goal_id"],
            event_type="TASK_STATE_CHANGED",
            entity_id=task_id,
            entity_state_revision=updated["state_revision"],
            resource_type="TASK",
        )
        _ = _task_from_row(updated)
        return _command_write(
            db,
            project_id=task["project_id"],
            goal_id=task["goal_id"],
            kind="CANCEL_TASK",
            request_scope=request_scope,
            request_digest=request_digest,
            result=CommandResult(task_id=task_id, final_status=final),
            subject=subject,
        )


def retry_task(
    engine: Engine,
    task_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ControlRequest,
) -> CommandOperation:
    """FAILED/CANCELLED → 新建 replacement Task，保留 work_lineage_id。"""
    path = f"/api/v1/tasks/{task_id}/retry"
    request_scope = json.dumps([str(task_id), subject, "POST", path, key], separators=(",", ":"))
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
        task = (
            db.execute(text("SELECT * FROM tasks WHERE id=:id FOR UPDATE"), {"id": task_id})
            .mappings()
            .first()
        )
        if task is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, task["project_id"], subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": task["project_id"]},
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
        goal = (
            db.execute(
                text("SELECT * FROM goals WHERE id=:id FOR UPDATE"),
                {"id": task["goal_id"]},
            )
            .mappings()
            .one()
        )
        if goal["status"] != "RUNNING":
            raise InvalidGoalState(goal["status"])
        barrier = db.execute(
            text(
                """SELECT 1 FROM finalization_barriers
                WHERE goal_id=:goal AND status IN ('DRAINING','SEALED','RELEASED')
                LIMIT 1"""
            ),
            {"goal": goal["id"]},
        ).first()
        if barrier is not None:
            raise InvalidGoalState("VERIFYING")
        if task["status"] not in ("FAILED", "CANCELLED"):
            raise InvalidTaskState(task["status"])
        if task["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()

        max_rounds = int(task["contract"]["retry_policy"]["max_execution_rounds"])
        next_round = int(task["execution_round"]) + 1
        if next_round > max_rounds:
            raise InvalidTaskState("EXECUTION_ROUNDS_EXHAUSTED")

        # 同 lineage 已有非终态 replacement 时拒绝再开。
        open_rep = db.execute(
            text(
                """SELECT 1 FROM tasks
                WHERE work_lineage_id=:lineage
                  AND status NOT IN ('DONE','FAILED','CANCELLED')
                  AND id<>:id
                LIMIT 1"""
            ),
            {"lineage": task["work_lineage_id"], "id": task_id},
        ).first()
        if open_rep is not None:
            raise InvalidTaskState("LINEAGE_OPEN")

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

        new_id = uuid4()
        contract = task["contract"]
        contract_digest = task["contract_digest"]
        # 合同字节不变则 digest 不变；仍写入新行。
        if _task_digest(contract) != contract_digest:
            contract_digest = _task_digest(contract)
        resources = Resources.model_validate(contract["resources"])
        binding = ExecutionBinding(
            goal_contract_revision=goal["contract_revision"],
            goal_contract_digest=goal["contract_digest"],
            task_contract_revision=task["contract_revision"],
            task_contract_digest=contract_digest,
            plan_revision=task["plan_revision"],
            subject_digest=contract_digest,
            policy_digest=policy_digest,
            model_profile_digest=model_digest,
            skill_set_digest=skill_digest,
        )
        db.execute(
            text(
                """INSERT INTO tasks(
                  id,project_id,goal_id,contract,contract_digest,contract_revision,plan_revision,
                  state_revision,status,work_lineage_id,execution_round,replaces_task_id)
                VALUES(
                  :id,:project,:goal,CAST(:contract AS jsonb),:digest,:crev,:prev,
                  1,'READY',:lineage,:round,:replaces)"""
            ),
            {
                "id": new_id,
                "project": task["project_id"],
                "goal": task["goal_id"],
                "contract": json.dumps(contract),
                "digest": contract_digest,
                "crev": task["contract_revision"],
                "prev": task["plan_revision"],
                "lineage": task["work_lineage_id"],
                "round": next_round,
                "replaces": task_id,
            },
        )
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:goal,'EXECUTE','TASK_WORK',:task,
                  CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,CAST(:resources AS jsonb))"""
            ),
            {
                "id": uuid4(),
                "project": task["project_id"],
                "goal": task["goal_id"],
                "task": new_id,
                "binding": binding.model_dump_json(),
                "resources": resources.model_dump_json(),
            },
        )
        append_goal_event(
            db,
            project_id=task["project_id"],
            goal_id=task["goal_id"],
            event_type="TASK_STATE_CHANGED",
            entity_id=new_id,
            entity_state_revision=1,
            resource_type="TASK",
        )
        return _command_write(
            db,
            project_id=task["project_id"],
            goal_id=task["goal_id"],
            kind="RETRY_TASK",
            request_scope=request_scope,
            request_digest=request_digest,
            result=CommandResult(
                replacement_task_id=new_id,
                work_lineage_id=task["work_lineage_id"],
            ),
            subject=subject,
        )
