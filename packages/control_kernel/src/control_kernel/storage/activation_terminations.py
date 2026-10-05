"""activation_terminations：per-attempt 确定性终止事实（M3.5）；≠ Goal DONE。

append-only；幂等键 attempt_id。HTTP 路径须带 lease 并校验 attempt 持有者。
不改 Goal 状态、不写 DONE。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..domain.activation_termination import (
    ACTIVATION_TERMINATION_REASONS,
    parse_activation_termination_reason,
)
from ..protocols.runtime import (
    LeaseIdentity,
    LeaseRejected,
    PlanRejected,
    StateRevisionConflict,
    WorkerForbidden,
)
from .policies import ScopeNotFound


def record_activation_termination(
    engine: Engine,
    *,
    subject: str,
    project_ids: list[str],
    activity_id: UUID,
    attempt_id: UUID,
    reason: str,
    detail: str | None = None,
    summary_artifact_id: UUID | None = None,
    closeout_artifact_id: UUID | None = None,
    lease: LeaseIdentity | None = None,
) -> dict[str, Any]:
    """写入 activation 终止事实；恒 marks_goal_done=False；禁止 Goal DONE。

    - 提交 subject 必须是已登记 ACTIVE worker
    - attempt 须属于 activity，且 goal 存在
    - 若提供 lease：须匹配 path/attempt，且 worker 为 attempt 持有者（零副作用身份门）
    - 幂等：同 attempt_id 已存在且 reason 一致 → 返回原行；reason 冲突 → PlanRejected
    """
    try:
        parsed = parse_activation_termination_reason(reason)
    except ValueError as exc:
        raise PlanRejected(str(exc)) from exc

    if detail is not None and (
        not str(detail).strip() or len(str(detail)) > 2000
    ):
        raise PlanRejected("detail 须为 1..2000 字符或省略")

    if lease is not None:
        if lease.activity_id != activity_id:
            raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
        if lease.attempt_id != attempt_id:
            raise LeaseRejected("INVALID_REQUEST", "租约 attempt 与请求不一致")

    with engine.begin() as db:
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
            raise WorkerForbidden("仅已登记 ACTIVE worker 可提交 activation 终止")

        attempt = (
            db.execute(
                text(
                    """SELECT a.id AS attempt_id, a.activity_id, a.worker_id,
                              a.fencing_epoch, a.status AS attempt_status,
                              act.project_id, act.goal_id, act.kind,
                              g.status AS goal_status
                       FROM activity_attempts a
                       INNER JOIN activities act ON act.id = a.activity_id
                       INNER JOIN goals g ON g.id = act.goal_id
                       WHERE a.id=:attempt AND a.activity_id=:activity
                       FOR SHARE OF a"""
                ),
                {"attempt": attempt_id, "activity": activity_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise ScopeNotFound()

        if lease is not None:
            # 身份不符：零副作用（未 INSERT）；迟到回执仍允许非 ACTIVE attempt
            if attempt["worker_id"] != worker["id"]:
                raise WorkerForbidden("仅 attempt 持有者可登记 activation 终止")
            if str(attempt["fencing_epoch"]) != str(lease.fencing_epoch):
                raise LeaseRejected("FENCING_REJECTED", "fencing_epoch 不匹配")

        # 项目成员或 ACTIVE worker 均可（编排 Activity 常无 JWT project_ids）
        from .policies import ConfigurationVersions

        try:
            ConfigurationVersions.check_scope(
                db, attempt["project_id"], subject, project_ids
            )
        except ScopeNotFound:
            # worker 已校验 ACTIVE，允许
            pass

        if attempt["goal_status"] == "DONE":
            pass

        existing = (
            db.execute(
                text(
                    """SELECT * FROM activation_terminations
                    WHERE attempt_id=:attempt"""
                ),
                {"attempt": attempt_id},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["reason"] != parsed.reason:
                raise PlanRejected(
                    "ACTIVATION_TERMINATION_CONFLICT: 同 attempt 原因不一致"
                )
            return _row_to_dict(existing)

        term_id = uuid4()
        db.execute(
            text(
                """INSERT INTO activation_terminations (
                    id, project_id, goal_id, activity_id, attempt_id,
                    reason, detail, summary_artifact_id, closeout_artifact_id,
                    worker_id
                ) VALUES (
                    :id, :project, :goal, :activity, :attempt,
                    :reason, :detail, :summary, :closeout,
                    :worker
                )"""
            ),
            {
                "id": term_id,
                "project": attempt["project_id"],
                "goal": attempt["goal_id"],
                "activity": activity_id,
                "attempt": attempt_id,
                "reason": parsed.reason,
                "detail": detail.strip() if detail else None,
                "summary": summary_artifact_id,
                "closeout": closeout_artifact_id,
                "worker": worker["id"],
            },
        )

        # 显式守卫：本事务不得把 Goal 写成 DONE
        goal_after = (
            db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": attempt["goal_id"]},
            )
            .mappings()
            .one()
        )
        if goal_after["status"] == "DONE" and attempt["goal_status"] != "DONE":
            raise PlanRejected("禁止 activation 终止路径写 Goal DONE")

        # M4：复盘确认可解除 NO_PROGRESS 对最终屏障的挡住（仍 ≠ Goal DONE）
        if parsed.reason == "GOAL_REQUIRES_REVIEW":
            from .finalization import try_seal_draining_barrier

            try_seal_draining_barrier(db, attempt["goal_id"])

        row = (
            db.execute(
                text("SELECT * FROM activation_terminations WHERE id=:id"),
                {"id": term_id},
            )
            .mappings()
            .one()
        )
        return _row_to_dict(row)


def goal_has_unresolved_no_progress_stop(db: Connection, goal_id: UUID) -> bool:
    """Goal 上是否仍有未经复盘确认的无进展 ForceStop。

    最新一条 ``NO_PROGRESS_STOP`` 之后若不存在 ``GOAL_REQUIRES_REVIEW``，
    则最终屏障不得 SEAL（M4 消费缝；≠ Goal DONE）。
    """
    stop = (
        db.execute(
            text(
                """SELECT created_at FROM activation_terminations
                WHERE goal_id=:goal AND reason='NO_PROGRESS_STOP'
                ORDER BY created_at DESC, id DESC
                LIMIT 1"""
            ),
            {"goal": goal_id},
        )
        .mappings()
        .first()
    )
    if stop is None:
        return False
    review = db.execute(
        text(
            """SELECT 1 FROM activation_terminations
            WHERE goal_id=:goal AND reason='GOAL_REQUIRES_REVIEW'
              AND created_at >= :stop_at
            LIMIT 1"""
        ),
        {"goal": goal_id, "stop_at": stop["created_at"]},
    ).first()
    return review is None


def list_activation_terminations(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
) -> list[dict[str, Any]]:
    """只读列出 Goal 下 activation 终止事实。"""
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
        from .policies import ConfigurationVersions

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
                    """SELECT * FROM activation_terminations
                    WHERE goal_id=:goal
                    ORDER BY created_at DESC, id DESC"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .all()
        )
        return [_row_to_dict(r) for r in rows]


def acknowledge_no_progress_review(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    expected_state_revision: int,
    detail: str | None = None,
) -> dict[str, Any]:
    """Operator/项目成员确认无进展复盘：落 GOAL_REQUIRES_REVIEW；≠ Goal DONE。

    - 须存在未复盘 NO_PROGRESS_STOP；已复盘则幂等返回既有 REVIEW 行
    - 另插 COMPLETED attempt（同 activity），避免同 attempt 原因冲突
    - 可触发 try_seal；仍禁止写 Goal DONE
    """
    if detail is not None and (
        not str(detail).strip() or len(str(detail)) > 2000
    ):
        raise PlanRejected("detail 须为 1..2000 字符或省略")

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

        from .policies import ConfigurationVersions

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
                raise WorkerForbidden("仅项目成员或 ACTIVE worker 可确认无进展复盘") from None

        if int(goal["state_revision"]) != int(expected_state_revision):
            raise StateRevisionConflict()

        if goal["status"] == "DONE":
            raise PlanRejected("Goal 已 DONE，拒绝再登记复盘确认")

        stop = (
            db.execute(
                text(
                    """SELECT * FROM activation_terminations
                    WHERE goal_id=:goal AND reason='NO_PROGRESS_STOP'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .first()
        )
        if stop is None:
            raise PlanRejected(
                "NO_PROGRESS_STOP_ABSENT: 无 NO_PROGRESS_STOP，无需复盘确认"
            )

        existing_review = (
            db.execute(
                text(
                    """SELECT * FROM activation_terminations
                    WHERE goal_id=:goal AND reason='GOAL_REQUIRES_REVIEW'
                      AND created_at >= :stop_at
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1"""
                ),
                {"goal": goal_id, "stop_at": stop["created_at"]},
            )
            .mappings()
            .first()
        )
        if existing_review is not None:
            return _row_to_dict(existing_review)

        review_attempt_id = uuid4()
        inserted = db.execute(
            text(
                """INSERT INTO activity_attempts(
                  id,activity_id,project_id,worker_id,binding_digest,fencing_epoch,
                  lease_expires_at,renewal_seq,status,skill_versions,started_at,finished_at)
                SELECT :id, a.activity_id, a.project_id, a.worker_id, a.binding_digest,
                       (SELECT COALESCE(MAX(x.fencing_epoch), 0) + 1
                        FROM activity_attempts x WHERE x.activity_id = a.activity_id),
                       a.lease_expires_at, 0, 'COMPLETED',
                       a.skill_versions, a.started_at, clock_timestamp()
                FROM activity_attempts a WHERE a.id=:old
                RETURNING id"""
            ),
            {"id": review_attempt_id, "old": stop["attempt_id"]},
        ).first()
        if inserted is None:
            raise PlanRejected("无法为复盘确认创建 attempt（源 attempt 缺失）")

        ack_detail = (detail.strip() if detail else "operator_no_progress_review_ack")
        # 审计：谁确认（≠ 改 marks_goal_done）
        if f"acked_by={subject}" not in ack_detail:
            suffix = f"acked_by={subject}"
            ack_detail = (
                f"{ack_detail};{suffix}"
                if len(ack_detail) + len(suffix) + 1 <= 2000
                else ack_detail[: 2000 - len(suffix) - 1] + ";" + suffix
            )

        term_id = uuid4()
        db.execute(
            text(
                """INSERT INTO activation_terminations (
                    id, project_id, goal_id, activity_id, attempt_id,
                    reason, detail, summary_artifact_id, closeout_artifact_id,
                    worker_id
                ) VALUES (
                    :id, :project, :goal, :activity, :attempt,
                    'GOAL_REQUIRES_REVIEW', :detail, NULL, NULL,
                    :worker
                )"""
            ),
            {
                "id": term_id,
                "project": stop["project_id"],
                "goal": goal_id,
                "activity": stop["activity_id"],
                "attempt": review_attempt_id,
                "detail": ack_detail,
                "worker": stop["worker_id"],
            },
        )

        goal_after = (
            db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        if goal_after["status"] == "DONE" and goal["status"] != "DONE":
            raise PlanRejected("禁止无进展复盘确认路径写 Goal DONE")

        from .finalization import try_seal_draining_barrier

        try_seal_draining_barrier(db, goal_id)

        # 再次守卫：try_seal 不得把 Goal 写成 DONE（FINALIZE 才可能后续裁决）
        goal_final = (
            db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        if goal_final["status"] == "DONE" and goal["status"] != "DONE":
            raise PlanRejected("禁止无进展复盘确认路径写 Goal DONE")

        row = (
            db.execute(
                text("SELECT * FROM activation_terminations WHERE id=:id"),
                {"id": term_id},
            )
            .mappings()
            .one()
        )
        return _row_to_dict(row)


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "goal_id": row["goal_id"],
        "activity_id": row["activity_id"],
        "attempt_id": row["attempt_id"],
        "reason": row["reason"],
        "detail": row["detail"],
        "summary_artifact_id": row["summary_artifact_id"],
        "closeout_artifact_id": row["closeout_artifact_id"],
        "worker_id": row["worker_id"],
        "created_at": row["created_at"],
        "marks_goal_done": False,
    }


# 供测试与文档引用
assert "NO_PROGRESS_STOP" in ACTIVATION_TERMINATION_REASONS
