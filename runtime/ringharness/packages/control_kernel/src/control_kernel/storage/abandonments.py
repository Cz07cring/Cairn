"""编排放弃恢复的 Kernel 持久化入口（Issue #24）。

Workflow 产出 RECOVERY_ABANDONED 后必须写入业务事实，否则 Goal 静默卡住且
与健康非终态不可区分。放弃 ≠ DONE；落 BLOCKED 供人工介入；对 RUNNING
ACTIVE attempt 发出 SHUTDOWN Stop（仅 REQUESTED，不杀进程）。
人工可 list 放弃行，并在 Stop 确认且无 RUNNING 后解除 BLOCKED（恢复 previous_status）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..protocols.runtime import PlanRejected, StateRevisionConflict, WorkerForbidden
from .events import append_goal_event
from .policies import ConfigurationVersions, ScopeNotFound

# 与 temporal_workflows / recovery_intent 对齐的可接受 reason
_ABANDON_REASONS = frozenset(
    {
        "INTENT_EXPIRED",
        "CHECKPOINT_SCHEMA_INCOMPATIBLE",
        "RECOVERY_ATTEMPTS_EXCEEDED",
        "RECOVERY_DISABLED",
        "WALL_CLOCK_BUDGET_EXHAUSTED",
        "BUDGET_READING_UNKNOWN",
    }
)

_TERMINAL = frozenset({"DONE", "FAILED", "CANCELLED"})
_ENGINEERING_KINDS = ("PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE")


def record_orchestration_abandonment(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    reason: str,
    generation: int,
    prior_run_id: str | None = None,
) -> dict[str, Any]:
    """把编排放弃裁决持久化为 append-only 行，并将非终态 Goal 置 BLOCKED。

    - 提交 subject 必须是已登记 ACTIVE worker（绑定身份，非自报）
    - 幂等键：(goal_id, generation)
    - 绝不写 Goal/Task DONE
    """
    if reason not in _ABANDON_REASONS:
        raise PlanRejected(f"未知放弃原因: {reason}")
    if int(generation) < 0:
        raise PlanRejected("generation 必须 >= 0")

    with engine.begin() as db:
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
        # 项目成员，或已登记 ACTIVE worker（编排 Activity 不依赖 JWT project_ids）
        try:
            ConfigurationVersions.check_scope(
                db, goal["project_id"], subject, project_ids
            )
        except ScopeNotFound:
            worker_probe = (
                db.execute(
                    text(
                        """SELECT id FROM workers
                        WHERE subject=:subject AND status='ACTIVE'"""
                    ),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker_probe is None:
                raise ScopeNotFound()

        worker = (
            db.execute(
                text(
                    """SELECT id FROM workers
                    WHERE subject=:subject AND status='ACTIVE' FOR SHARE"""
                ),
                {"subject": subject},
            )
            .mappings()
            .first()
        )
        if worker is None:
            raise WorkerForbidden("仅已登记 ACTIVE worker 可提交编排放弃")

        existing = (
            db.execute(
                text(
                    """SELECT * FROM orchestration_abandonments
                    WHERE goal_id=:goal AND generation=:gen"""
                ),
                {"goal": goal_id, "gen": int(generation)},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["reason"] != reason:
                raise PlanRejected(
                    "ORCHESTRATION_ABANDON_CONFLICT: 同 generation 原因不一致"
                )
            if prior_run_id is not None and existing["prior_run_id"] not in (
                None,
                prior_run_id,
            ):
                raise PlanRejected(
                    "ORCHESTRATION_ABANDON_CONFLICT: 同 generation prior_run_id 不一致"
                )
            return _row_to_dict(existing)

        if goal["status"] in _TERMINAL:
            raise PlanRejected(
                f"Goal 已终态 {goal['status']}，拒绝编排放弃写入"
            )

        abandon_id = uuid4()
        block_reason = f"ORCHESTRATION_ABANDONED:{reason}"
        db.execute(
            text(
                """INSERT INTO orchestration_abandonments(
                  id,project_id,goal_id,generation,reason,prior_run_id,worker_id)
                VALUES(
                  :id,:project,:goal,:gen,:reason,:run,:worker)"""
            ),
            {
                "id": abandon_id,
                "project": goal["project_id"],
                "goal": goal_id,
                "gen": int(generation),
                "reason": reason,
                "run": prior_run_id,
                "worker": worker["id"],
            },
        )

        # 关闭新工程准入：取消尚未领取的 READY；RUNNING 发 SHUTDOWN Stop 排空（不杀进程）
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE goal_id=:goal AND status='READY'
                  AND kind = ANY(:kinds)"""
            ),
            {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
        )
        from .control_commands import _emit_stops_for_running_attempts

        _emit_stops_for_running_attempts(db, goal_id, reason="SHUTDOWN")

        updated = (
            db.execute(
                text(
                    """UPDATE goals SET status='BLOCKED', previous_status=:prev,
                        block_reason=:reason,
                        state_revision=state_revision+1, updated_at=clock_timestamp()
                    WHERE id=:id AND status NOT IN ('DONE','FAILED','CANCELLED')
                    RETURNING *"""
                ),
                {
                    "id": goal_id,
                    "prev": goal["status"],
                    "reason": block_reason,
                },
            )
            .mappings()
            .first()
        )
        if updated is None:
            # 并发终态：放弃行已写入但 Goal 未改——仍返回行，调用方可见冲突需再读
            row = (
                db.execute(
                    text(
                        """SELECT * FROM orchestration_abandonments WHERE id=:id"""
                    ),
                    {"id": abandon_id},
                )
                .mappings()
                .one()
            )
            return _row_to_dict(row)

        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            event_type="GOAL_STATE_CHANGED",
            entity_id=goal_id,
            entity_state_revision=updated["state_revision"],
            resource_type="GOAL",
        )
        row = (
            db.execute(
                text(
                    """SELECT * FROM orchestration_abandonments WHERE id=:id"""
                ),
                {"id": abandon_id},
            )
            .mappings()
            .one()
        )
        return _row_to_dict(row)


def list_orchestration_abandonments(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
) -> list[dict[str, Any]]:
    """只读列出 Goal 的编排放弃事实（人工介入可观察）。"""
    with engine.begin() as db:
        goal = (
            db.execute(
                text("SELECT project_id FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if goal is None:
            raise ScopeNotFound()
        try:
            ConfigurationVersions.check_scope(
                db, goal["project_id"], subject, project_ids
            )
        except ScopeNotFound:
            worker_probe = (
                db.execute(
                    text(
                        """SELECT id FROM workers
                        WHERE subject=:subject AND status='ACTIVE'"""
                    ),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker_probe is None:
                raise ScopeNotFound() from None
        rows = (
            db.execute(
                text(
                    """SELECT * FROM orchestration_abandonments
                    WHERE goal_id=:goal
                    ORDER BY created_at DESC, id DESC"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .all()
        )
        return [_row_to_dict(r) for r in rows]


def release_orchestration_abandonment_block(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    expected_state_revision: int,
) -> dict[str, Any]:
    """人工解除 ORCHESTRATION_ABANDONED BLOCKED，恢复 previous_status。

    - 须无未确认 SHUTDOWN Stop、无 RUNNING 工程活动
    - 绝不写 DONE；previous_status 缺失则失败关闭
    """
    with engine.begin() as db:
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
        try:
            ConfigurationVersions.check_scope(
                db, goal["project_id"], subject, project_ids
            )
        except ScopeNotFound:
            worker_probe = (
                db.execute(
                    text(
                        """SELECT id FROM workers
                        WHERE subject=:subject AND status='ACTIVE'"""
                    ),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker_probe is None:
                raise WorkerForbidden() from None
        if goal["status"] != "BLOCKED":
            raise PlanRejected(f"Goal 状态为 {goal['status']}，非 BLOCKED")
        reason = goal["block_reason"] or ""
        if not reason.startswith("ORCHESTRATION_ABANDONED:"):
            raise PlanRejected("block_reason 非编排放弃，拒绝本入口解除")
        if int(goal["state_revision"]) != int(expected_state_revision):
            raise StateRevisionConflict()
        open_stop = db.execute(
            text(
                """SELECT 1 FROM stops s
                JOIN activities a ON a.id = s.activity_id
                WHERE a.goal_id = :goal
                  AND s.reason = 'SHUTDOWN'
                  AND s.status IN ('REQUESTED', 'UNCONFIRMED')
                LIMIT 1"""
            ),
            {"goal": goal_id},
        ).first()
        if open_stop is not None:
            raise PlanRejected(
                "STOP_UNCONFIRMED: 仍有未确认 SHUTDOWN Stop，禁止解除 BLOCKED"
            )
        running = db.execute(
            text(
                """SELECT 1 FROM activities
                WHERE goal_id=:goal AND status='RUNNING'
                  AND kind = ANY(:kinds)
                LIMIT 1"""
            ),
            {"goal": goal_id, "kinds": list(_ENGINEERING_KINDS)},
        ).first()
        if running is not None:
            raise PlanRejected("仍有 RUNNING 工程活动，禁止解除 BLOCKED")
        prev = goal["previous_status"]
        if prev is None or prev in _TERMINAL or prev == "BLOCKED":
            raise PlanRejected("previous_status 不可恢复，拒绝解除")
        updated = (
            db.execute(
                text(
                    """UPDATE goals SET status=:status, previous_status='BLOCKED',
                        block_reason=NULL,
                        state_revision=state_revision+1, updated_at=clock_timestamp()
                    WHERE id=:id AND status='BLOCKED'
                    RETURNING *"""
                ),
                {"id": goal_id, "status": prev},
            )
            .mappings()
            .first()
        )
        if updated is None:
            raise PlanRejected("Goal 状态竞态，解除失败")
        if updated["status"] == "DONE":
            raise PlanRejected("禁止将放弃解除写为 DONE")
        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            event_type="GOAL_STATE_CHANGED",
            entity_id=goal_id,
            entity_state_revision=updated["state_revision"],
            resource_type="GOAL",
        )
        return {
            "id": updated["id"],
            "status": updated["status"],
            "block_reason": updated["block_reason"],
            "state_revision": int(updated["state_revision"]),
            "previous_status": updated["previous_status"],
            "marks_goal_done": False,
        }


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "goal_id": row["goal_id"],
        "generation": int(row["generation"]),
        "reason": row["reason"],
        "prior_run_id": row["prior_run_id"],
        "worker_id": row["worker_id"],
        "created_at": row["created_at"],
        "marks_goal_done": False,
    }
