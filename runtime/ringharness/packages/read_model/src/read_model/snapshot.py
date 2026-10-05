"""ReadModel：一致读 GoalSnapshot / 事件游标 / SystemStatus。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from control_kernel.protocols.read_model import (
    ComponentStatus,
    GoalSnapshot,
    ResourcePool,
    ResourceStatus,
    SystemStatus,
    TrustState,
)
from control_kernel.storage.activities import _activity_from_row, _command_from_row
from control_kernel.storage.approvals import _from_row as _approval_from_row
from control_kernel.storage.effects import _effect_from_row
from control_kernel.storage.events import latest_seq, list_events_after
from control_kernel.storage.feedback import list_feedback_for_goal
from control_kernel.storage.finalization import get_barrier_for_goal
from control_kernel.storage.goals import _row_to_resource
from control_kernel.storage.plans import _plan_from_row, _task_from_row
from control_kernel.storage.policies import ConfigurationVersions, ScopeNotFound
from sqlalchemy import Engine, text

SNAPSHOT_LIMIT = 1000
logger = logging.getLogger(__name__)


class EventCursorExpired(Exception):
    """after_seq 落后于保留窗口（当前实现：seq 不存在且小于最新）。"""


def goal_snapshot(
    engine: Engine, goal_id: UUID, subject: str, project_ids: list[str]
) -> GoalSnapshot:
    # snapshot 会分多次读取 Goal、计划、运行对象与 latest_seq。必须固定在同一
    # PostgreSQL 快照，否则并发提交可能形成“旧对象 + 新游标”，客户端随后会
    # 从过新的 seq 订阅并漏掉使这些对象失效的事件。
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
        row = (
            db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": goal_id})
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
        goal = _row_to_resource(row, get_barrier_for_goal(db, goal_id))

        plan_row = (
            db.execute(
                text(
                    """SELECT * FROM plans
                    WHERE goal_id=:goal AND status='PUBLISHED'
                    ORDER BY plan_revision DESC NULLS LAST, created_at DESC
                    LIMIT 1"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .first()
        )
        plan = _plan_from_row(plan_row) if plan_row else None

        tasks = (
            [
                _task_from_row(r)
                for r in db.execute(
                    text(
                        """SELECT * FROM tasks
                        WHERE goal_id=:goal AND plan_revision=:plan_revision
                        ORDER BY created_at,id LIMIT :limit"""
                    ),
                    {
                        "goal": goal_id,
                        "plan_revision": plan.plan_revision,
                        "limit": SNAPSHOT_LIMIT,
                    },
                ).mappings()
            ]
            if plan is not None
            else []
        )
        activities = [
            _activity_from_row(r)
            for r in db.execute(
                text(
                    """SELECT * FROM activities
                    WHERE goal_id=:goal
                    AND status IN ('PENDING','READY','RUNNING','WAITING','RECOVERING')
                    ORDER BY created_at,id LIMIT :limit"""
                ),
                {"goal": goal_id, "limit": SNAPSHOT_LIMIT},
            ).mappings()
        ]
        effects = [
            _effect_from_row(r)
            for r in db.execute(
                text(
                    """SELECT * FROM effect_intents WHERE goal_id=:goal
                    AND status IN ('PREPARED','AUTHORIZED','DISPATCHED','UNKNOWN')
                    ORDER BY created_at,id LIMIT :limit"""
                ),
                {"goal": goal_id, "limit": SNAPSHOT_LIMIT},
            ).mappings()
        ]
        commands = [
            _command_from_row(r)
            for r in db.execute(
                text(
                    """SELECT * FROM command_operations
                    WHERE goal_id=:goal AND status IN ('ACCEPTED','RUNNING')
                    ORDER BY created_at,id LIMIT :limit"""
                ),
                {"goal": goal_id, "limit": SNAPSHOT_LIMIT},
            ).mappings()
        ]
        approvals = [
            _approval_from_row(r)
            for r in db.execute(
                text(
                    """SELECT * FROM approvals
                    WHERE goal_id=:goal
                    AND (status='PENDING'
                      OR (status='APPROVED' AND consumed_subject_id IS NULL))
                    ORDER BY created_at,id LIMIT :limit"""
                ),
                {"goal": goal_id, "limit": SNAPSHOT_LIMIT},
            ).mappings()
        ]
        # PlanningFeedback：最新 100 条；超限标记 truncated。
        feedbacks, feedback_truncated = list_feedback_for_goal(db, goal_id, limit=100)
        seq = latest_seq(db, goal_id)
        return GoalSnapshot(
            goal=goal,
            plan=plan,
            tasks=tasks,
            activities=activities,
            approvals=approvals,
            effects=effects,
            commands=commands,
            planning_feedback=feedbacks,
            feedback_truncated=feedback_truncated,
            latest_seq=seq,
        )


def goal_events(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    *,
    after_seq: int,
    limit: int,
) -> list[dict]:
    if after_seq < 0:
        raise ValueError("after_seq 无效")
    with engine.connect() as db:
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
        ConfigurationVersions.check_scope(db, goal["project_id"], subject, project_ids)
        max_seq = int(latest_seq(db, goal_id))
        if after_seq > 0 and after_seq > max_seq:
            # 客户端游标超前：返回空，不 410。
            return []
        if after_seq > 0:
            exists = db.execute(
                text("SELECT 1 FROM goal_events WHERE goal_id=:g AND seq=:s"),
                {"g": goal_id, "s": after_seq},
            ).first()
            if exists is None and after_seq < max_seq:
                # 中间缺口视为游标过期（未来 GC 后的路径）；当前无 GC 时极少触发。
                raise EventCursorExpired()
        return list_events_after(db, goal_id, after_seq, limit)


def system_status(
    engine: Engine,
    project_id: UUID,
    subject: str,
    project_ids: list[str],
    *,
    object_store_probe: Callable[[], None] | None,
) -> SystemStatus:
    now = datetime.now(UTC)
    with engine.connect() as db:
        ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
        trust_row = (
            db.execute(
                text(
                    """SELECT trust_revision,status,decision_ids,propagation_job_id
                    FROM project_trust_states
                    WHERE project_id=:id"""
                ),
                {"id": project_id},
            )
            .mappings()
            .first()
        )
        if trust_row is None:
            raise ScopeNotFound()
        try:
            db.execute(text("SELECT 1")).scalar_one()
            pg_state = "HEALTHY"
            pg_reason = None
        except Exception:  # noqa: BLE001 — 探测连通性，任意失败均视为不可用
            pg_state = "UNAVAILABLE"
            pg_reason = "DATABASE_UNREACHABLE"
        queued = db.execute(
            text(
                """SELECT COUNT(*) FROM activities
                WHERE project_id=:p AND status='READY'"""
            ),
            {"p": project_id},
        ).scalar_one()
        resource_usage = (
            db.execute(
                text(
                    """SELECT
                      COALESCE(SUM((resources->>'model_slots')::int),0) AS model_used,
                      COALESCE(SUM((resources->>'browser_slots')::int),0) AS browser_used
                    FROM resource_reservations
                    WHERE project_id=:p AND status='HELD'"""
                ),
                {"p": project_id},
            )
            .mappings()
            .one()
        )
        quarantined = db.execute(
            text(
                """SELECT COUNT(*) FROM resource_reservations
                WHERE project_id=:p AND status='QUARANTINED'"""
            ),
            {"p": project_id},
        ).scalar_one()
        from control_kernel.storage.obligations import count_quarantined_obligations

        quarantined_obligations = count_quarantined_obligations(db, project_id)
    if object_store_probe is None:
        object_state = "UNAVAILABLE"
        object_reason = "OBJECT_STORE_UNCONFIGURED"
    else:
        try:
            object_store_probe()
        except Exception:  # adapter 探针失败统一降级，详情只进内部日志
            logger.warning("object-store-health-probe-failed", exc_info=True)
            object_state = "UNAVAILABLE"
            object_reason = "OBJECT_STORE_UNREACHABLE"
        else:
            object_state = "HEALTHY"
            object_reason = None
    obl_state = "HEALTHY" if quarantined_obligations == 0 else "DEGRADED"
    obl_reason = (
        None
        if quarantined_obligations == 0
        else f"QUARANTINED_OBLIGATIONS:{quarantined_obligations}"
    )
    return SystemStatus(
        trust=TrustState(
            project_id=project_id,
            trust_revision=str(trust_row["trust_revision"]),
            status=trust_row["status"],
            decision_ids=list(trust_row["decision_ids"] or []),
            propagation_job_id=trust_row["propagation_job_id"],
        ),
        components=[
            ComponentStatus(
                name="postgres", state=pg_state, observed_at=now, reason_code=pg_reason
            ),
            ComponentStatus(
                name="object_store",
                state=object_state,
                observed_at=now,
                reason_code=object_reason,
            ),
            ComponentStatus(
                name="control_api", state="HEALTHY", observed_at=now, reason_code=None
            ),
            ComponentStatus(
                name="verification_obligation_quarantine",
                state=obl_state,
                observed_at=now,
                reason_code=obl_reason,
            ),
        ],
        resources=ResourceStatus(
            model=ResourcePool(used=int(resource_usage["model_used"]), total=0),
            tools=ResourcePool(used=0, total=0),
            browsers=ResourcePool(used=int(resource_usage["browser_used"]), total=0),
            queued_activities=int(queued),
            quarantined_resources=int(quarantined),
            observed_at=now,
            # 当前没有权威容量心跳；占用量来自 PG，但 total=0 不能解释为实时零容量。
            stale=True,
        ),
        observed_at=now,
        stale=False,
    )
