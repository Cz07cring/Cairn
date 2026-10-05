"""Goal pause / resume / cancel：排空后才终态；UNKNOWN 阻止假终态。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..protocols.runtime import (
    CommandOperation,
    CommandResult,
    ControlRequest,
    InvalidGoalState,
    StateRevisionConflict,
    StopRequest,
)
from .activities import _command_from_row
from .events import append_goal_event
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict
from .stops import insert_stop_request

_ENGINEERING_KINDS = ("PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE")


def _emit_stops_for_running_attempts(
    db: Connection, goal_id: UUID, *, reason: Literal["PAUSE", "CANCEL", "SHUTDOWN", "TRUST_INVALIDATION"]
) -> None:
    """对 RUNNING + ACTIVE current attempt 发出 StopRequest（仅 REQUESTED）。

    不杀进程；终态排空见 maybe_complete_pause_or_cancel（须等 PAUSE/CANCEL Stop 确认）。
    TRUST_INVALIDATION：信任传播排空旧执行者（doc/05 §3.11），不假取消 RUNNING。
    """
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
        insert_stop_request(
            db,
            StopRequest(
                request_id=uuid4(),
                activation_id=attempt_id,
                activity_id=row["activity_id"],
                attempt_id=attempt_id,
                fencing_epoch=str(row["fencing_epoch"]),
                reason=reason,
                deadline_at=deadline,
            ),
            row["project_id"],
        )


def _count_unknown_or_dispatched(db: Connection, goal_id: UUID) -> int:
    return int(
        db.execute(
            text(
                """SELECT count(*) FROM effect_intents e
                JOIN activities a ON a.id=e.activity_id
                WHERE a.goal_id=:goal AND e.status IN ('DISPATCHED','UNKNOWN')"""
            ),
            {"goal": goal_id},
        ).scalar_one()
    )


def _count_running(db: Connection, goal_id: UUID) -> int:
    return int(
        db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND status='RUNNING'
                  AND kind = ANY(:kinds)"""
            ),
            {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
        ).scalar_one()
    )


def _cancel_ready(db: Connection, goal_id: UUID) -> None:
    db.execute(
        text(
            """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
            WHERE goal_id=:goal AND status='READY' AND kind = ANY(:kinds)"""
        ),
        {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
    )


def _wake_paused_waiting(db: Connection, goal_id: UUID) -> None:
    db.execute(
        text(
            """UPDATE activities SET status='READY',
              wait_reason=NULL, resume_state=NULL, wake_at=NULL, wait_deadline_at=NULL,
              updated_at=clock_timestamp()
            WHERE goal_id=:goal AND status='WAITING' AND wait_reason='PAUSED'"""
        ),
        {"goal": goal_id},
    )


def maybe_complete_pause_or_cancel(db: Connection, goal_id: UUID) -> None:
    """活动/效果终态后调用：PAUSING→PAUSED 或 CANCELLING→CANCELLED。

    AB09 对齐：仍有 PAUSE/CANCEL 未确认 Stop 时不得假终态（LEASE_EXPIRED 不在此门）。
    """
    from .stops import goal_has_unconfirmed_control_stop

    goal = (
        db.execute(text("SELECT * FROM goals WHERE id=:id FOR UPDATE"), {"id": goal_id})
        .mappings()
        .first()
    )
    if goal is None:
        return
    if goal["status"] == "PAUSING":
        if _count_running(db, goal_id) > 0 or _count_unknown_or_dispatched(db, goal_id) > 0:
            return
        if goal_has_unconfirmed_control_stop(db, goal_id):
            return
        _cancel_ready(db, goal_id)
        # 保留 previous_status（进入 PAUSING 前的原阶段），供 resume 恢复。
        updated = (
            db.execute(
                text(
                    """UPDATE goals SET status='PAUSED',
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id RETURNING *"""
                ),
                {"id": goal_id},
            )
            .mappings()
            .one()
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
        return
    if goal["status"] == "CANCELLING":
        if _count_unknown_or_dispatched(db, goal_id) > 0:
            return
        if _count_running(db, goal_id) > 0:
            return
        if goal_has_unconfirmed_control_stop(db, goal_id):
            return
        _cancel_ready(db, goal_id)
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE goal_id=:goal AND status IN ('WAITING','RECOVERING','PENDING')
                  AND kind = ANY(:kinds)"""
            ),
            {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
        )
        updated = (
            db.execute(
                text(
                    """UPDATE goals SET status='CANCELLED', previous_status='CANCELLING',
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id RETURNING *"""
                ),
                {"id": goal_id},
            )
            .mappings()
            .one()
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


def _command_write(
    db: Connection,
    *,
    goal: dict,
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
                "project": goal["project_id"],
                "goal": goal["id"],
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


def pause_goal(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ControlRequest,
) -> CommandOperation:
    path = f"/api/v1/goals/{goal_id}/pause"
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
        if goal["status"] not in ("PLANNING", "RUNNING", "VERIFYING"):
            raise InvalidGoalState(goal["status"])
        if goal["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        from .budget_clock import advance_goal_budget_clock

        # Issue #20：进入暂停前推进；暂停后 elapsed 仍累计、active 停表
        advance_goal_budget_clock(db, goal_id)

        _cancel_ready(db, goal_id)
        draining = _count_running(db, goal_id) > 0 or _count_unknown_or_dispatched(db, goal_id) > 0
        if draining:
            updated = (
                db.execute(
                    text(
                        """UPDATE goals SET status='PAUSING', previous_status=:prev,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": goal_id, "prev": goal["status"]},
                )
                .mappings()
                .one()
            )
            final = "PAUSING"
            _emit_stops_for_running_attempts(db, goal_id, reason="PAUSE")
        else:
            updated = (
                db.execute(
                    text(
                        """UPDATE goals SET status='PAUSED', previous_status=:prev,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": goal_id, "prev": goal["status"]},
                )
                .mappings()
                .one()
            )
            final = "PAUSED"
        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            event_type="GOAL_STATE_CHANGED",
            entity_id=goal_id,
            entity_state_revision=updated["state_revision"],
            resource_type="GOAL",
        )
        return _command_write(
            db,
            goal=goal,
            kind="PAUSE",
            request_scope=request_scope,
            request_digest=request_digest,
            result=CommandResult(goal_id=goal_id, final_status=final),
            subject=subject,
        )


def resume_goal(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ControlRequest,
) -> CommandOperation:
    path = f"/api/v1/goals/{goal_id}/resume"
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
        if goal["status"] != "PAUSED":
            raise InvalidGoalState(goal["status"])
        if goal["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()

        from .budget_clock import advance_goal_budget_clock

        # Issue #20：resume 前推进——暂停期间 elapsed 已累计、不重置
        advance_goal_budget_clock(db, goal_id)

        # previous_status 在进入 PAUSING/PAUSED 时保存原阶段；完成排空时不覆盖它。
        target = goal["previous_status"]
        if target not in ("PLANNING", "RUNNING", "VERIFYING"):
            target = "RUNNING" if goal["plan_revision"] is not None else "PLANNING"

        _wake_paused_waiting(db, goal_id)
        updated = (
            db.execute(
                text(
                    """UPDATE goals SET status=:status, previous_status='PAUSED',
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id RETURNING *"""
                ),
                {"id": goal_id, "status": target},
            )
            .mappings()
            .one()
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
        return _command_write(
            db,
            goal=goal,
            kind="RESUME",
            request_scope=request_scope,
            request_digest=request_digest,
            result=CommandResult(goal_id=goal_id, final_status=target),
            subject=subject,
        )


def cancel_goal(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: ControlRequest,
) -> CommandOperation:
    path = f"/api/v1/goals/{goal_id}/cancel"
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
        if goal["status"] in ("CANCELLED", "DONE", "FAILED", "DRAFT"):
            raise InvalidGoalState(goal["status"])
        if goal["status"] == "CANCELLING":
            raise InvalidGoalState(goal["status"])
        if goal["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()

        _cancel_ready(db, goal_id)
        blocked = _count_unknown_or_dispatched(db, goal_id) > 0 or _count_running(db, goal_id) > 0
        if blocked:
            updated = (
                db.execute(
                    text(
                        """UPDATE goals SET status='CANCELLING', previous_status=:prev,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": goal_id, "prev": goal["status"]},
                )
                .mappings()
                .one()
            )
            final = "CANCELLING"
            _emit_stops_for_running_attempts(db, goal_id, reason="CANCEL")
        else:
            db.execute(
                text(
                    """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                    WHERE goal_id=:goal AND status IN ('WAITING','RECOVERING','PENDING')
                      AND kind = ANY(:kinds)"""
                ),
                {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
            )
            updated = (
                db.execute(
                    text(
                        """UPDATE goals SET status='CANCELLED', previous_status=:prev,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": goal_id, "prev": goal["status"]},
                )
                .mappings()
                .one()
            )
            final = "CANCELLED"
        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            event_type="GOAL_STATE_CHANGED",
            entity_id=goal_id,
            entity_state_revision=updated["state_revision"],
            resource_type="GOAL",
        )
        return _command_write(
            db,
            goal=goal,
            kind="CANCEL_GOAL",
            request_scope=request_scope,
            request_digest=request_digest,
            result=CommandResult(goal_id=goal_id, final_status=final),
            subject=subject,
        )
