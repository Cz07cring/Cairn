"""AB06：Goal 级 Nudge 预算持久账（进程重启可恢复）；≠ Goal DONE。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..protocols.runtime import PlanRejected, WorkerForbidden
from .policies import ScopeNotFound


def get_goal_nudge_budget(db: Connection, goal_id: UUID) -> dict[str, Any] | None:
    """只读：无行则 None（调用方视为 consumed=0）。"""
    row = (
        db.execute(
            text(
                """SELECT goal_id, project_id, consumed, max_budget, updated_at
                FROM goal_nudge_budgets WHERE goal_id=:goal"""
            ),
            {"goal": goal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        "goal_id": row["goal_id"],
        "project_id": row["project_id"],
        "consumed": int(row["consumed"]),
        "max_budget": int(row["max_budget"]),
        "updated_at": row["updated_at"],
        "marks_goal_done": False,
    }


def read_goal_nudge_budget(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    max_budget: int,
) -> dict[str, Any]:
    """读取（或默认空账）；不写库；≠ DONE。"""
    if not isinstance(max_budget, int) or max_budget < 1:
        raise PlanRejected("max_budget 须为 >=1 的整数")
    with engine.begin() as db:
        goal = _require_goal_scope(db, goal_id, subject=subject, project_ids=project_ids)
        row = get_goal_nudge_budget(db, goal_id)
        if row is None:
            return {
                "goal_id": goal_id,
                "project_id": goal["project_id"],
                "consumed": 0,
                "max_budget": max_budget,
                "updated_at": None,
                "marks_goal_done": False,
            }
        # 合同上界变更：返回钳制后的视图（不在只读路径写库）
        consumed = min(int(row["consumed"]), max_budget)
        return {
            **row,
            "consumed": consumed,
            "max_budget": max_budget,
            "marks_goal_done": False,
        }


def try_consume_goal_nudge_budget(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    max_budget: int,
) -> dict[str, Any]:
    """原子消耗 1 次 Nudge；已耗尽则 accepted=false 且不增；≠ Goal DONE。"""
    if not isinstance(max_budget, int) or max_budget < 1:
        raise PlanRejected("max_budget 须为 >=1 的整数")

    with engine.begin() as db:
        goal = _require_goal_scope(db, goal_id, subject=subject, project_ids=project_ids)
        if goal["status"] == "DONE":
            raise PlanRejected("Goal 已 DONE，拒绝再消耗 Nudge 预算")

        # 先对齐 max_budget（合同变更）
        existing = get_goal_nudge_budget(db, goal_id)
        if existing is not None and int(existing["max_budget"]) != max_budget:
            db.execute(
                text(
                    """UPDATE goal_nudge_budgets
                    SET max_budget=:max,
                        consumed=LEAST(consumed, :max),
                        updated_at=clock_timestamp()
                    WHERE goal_id=:goal"""
                ),
                {"goal": goal_id, "max": max_budget},
            )

        # INSERT 首消；冲突则有条件 +1
        inserted = (
            db.execute(
                text(
                    """INSERT INTO goal_nudge_budgets(
                      goal_id, project_id, consumed, max_budget)
                    VALUES (:goal, :project, 1, :max)
                    ON CONFLICT (goal_id) DO NOTHING
                    RETURNING consumed, max_budget"""
                ),
                {
                    "goal": goal_id,
                    "project": goal["project_id"],
                    "max": max_budget,
                },
            )
            .mappings()
            .first()
        )
        if inserted is not None:
            return {
                "goal_id": goal_id,
                "project_id": goal["project_id"],
                "consumed": int(inserted["consumed"]),
                "max_budget": int(inserted["max_budget"]),
                "accepted": True,
                "marks_goal_done": False,
            }

        updated = (
            db.execute(
                text(
                    """UPDATE goal_nudge_budgets
                    SET consumed = consumed + 1,
                        updated_at = clock_timestamp()
                    WHERE goal_id = :goal AND consumed < max_budget
                    RETURNING consumed, max_budget"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .first()
        )
        if updated is not None:
            return {
                "goal_id": goal_id,
                "project_id": goal["project_id"],
                "consumed": int(updated["consumed"]),
                "max_budget": int(updated["max_budget"]),
                "accepted": True,
                "marks_goal_done": False,
            }

        row = get_goal_nudge_budget(db, goal_id)
        assert row is not None
        return {
            "goal_id": goal_id,
            "project_id": goal["project_id"],
            "consumed": int(row["consumed"]),
            "max_budget": int(row["max_budget"]),
            "accepted": False,
            "marks_goal_done": False,
        }


def _require_goal_scope(
    db: Connection,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
):
    goal = (
        db.execute(
            text("SELECT id, project_id, status FROM goals WHERE id=:id FOR UPDATE"),
            {"id": goal_id},
        )
        .mappings()
        .first()
    )
    if goal is None:
        raise ScopeNotFound()
    from .policies import ConfigurationVersions

    try:
        ConfigurationVersions.check_scope(
            db, goal["project_id"], subject, project_ids
        )
    except ScopeNotFound:
        worker = (
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
        if worker is None:
            raise WorkerForbidden("仅项目成员或 ACTIVE worker 可访问 Nudge 预算") from None
    return goal
