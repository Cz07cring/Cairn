"""AB06：Goal 级软禁同参键持久账（进程重启可恢复）；≠ Goal DONE。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..protocols.runtime import PlanRejected, WorkerForbidden
from .policies import ScopeNotFound

_MAX_KEY_LEN = 512


def list_goal_ban_call_keys(db: Connection, goal_id: UUID) -> list[str]:
    rows = db.execute(
        text(
            """SELECT call_key FROM goal_ban_call_keys
            WHERE goal_id=:goal
            ORDER BY created_at ASC, call_key ASC"""
        ),
        {"goal": goal_id},
    ).all()
    return [str(r[0]) for r in rows]


def read_goal_ban_call_keys(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
) -> dict[str, Any]:
    """只读列出已软禁 call_key；≠ DONE。"""
    with engine.begin() as db:
        goal = _require_goal_scope(db, goal_id, subject=subject, project_ids=project_ids)
        keys = list_goal_ban_call_keys(db, goal_id)
        return {
            "goal_id": goal_id,
            "project_id": goal["project_id"],
            "call_keys": keys,
            "marks_goal_done": False,
        }


def add_goal_ban_call_key(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    call_key: str,
) -> dict[str, Any]:
    """幂等登记软禁键；已存在则 inserted=false；≠ DONE。"""
    key = (call_key or "").strip()
    if not key or len(key) > _MAX_KEY_LEN:
        raise PlanRejected(f"call_key 须为 1..{_MAX_KEY_LEN} 字符")

    with engine.begin() as db:
        goal = _require_goal_scope(db, goal_id, subject=subject, project_ids=project_ids)
        if goal["status"] == "DONE":
            raise PlanRejected("Goal 已 DONE，拒绝再登记软禁键")

        inserted = (
            db.execute(
                text(
                    """INSERT INTO goal_ban_call_keys(goal_id, project_id, call_key)
                    VALUES (:goal, :project, :key)
                    ON CONFLICT (goal_id, call_key) DO NOTHING
                    RETURNING call_key"""
                ),
                {
                    "goal": goal_id,
                    "project": goal["project_id"],
                    "key": key,
                },
            )
            .mappings()
            .first()
        )
        keys = list_goal_ban_call_keys(db, goal_id)
        return {
            "goal_id": goal_id,
            "project_id": goal["project_id"],
            "call_keys": keys,
            "call_key": key,
            "inserted": inserted is not None,
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
            raise WorkerForbidden(
                "仅项目成员或 ACTIVE worker 可访问软禁同参键"
            ) from None
    return goal
