"""StopRequest / StopReceipt 存储（05§3.6）；本切片不做进程杀伤。

身份约定（诚实切片）：
- activation_id 必须等于 attempt_id（与 Runner harnessPlanAdapter 一致）。
- resource_instance_id 单实例场景默认等于 attempt_id；调用方可显式传入 worker 侧实例 id。
- CONFIRMED 仅当观察为 EXITED 且 compute_released=true；ISOLATED 不单独确认。
- CONFIRMED 时将该 attempt 上 QUARANTINED/HELD 资源与预算置 RELEASED（R03/TR04）。
- Goal pause/cancel 进入 PAUSING/CANCELLING 时由 control_commands 调 insert_stop_request
  发出 REQUESTED；PAUSE/CANCEL Stop CONFIRMED（EXITED∧compute_released）时泊入
  WAITING(PAUSED) 或 CANCELLED，再 maybe_complete → PAUSED/CANCELLED。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..protocols.runtime import (
    PlanRejected,
    StopReceipt,
    StopReceiptAccepted,
    StopRequest,
    StopResource,
    WorkerForbidden,
)
from .policies import ScopeNotFound


def goal_has_unconfirmed_stop_barrier(db: Connection, goal_id: UUID) -> bool:
    """Goal 上仍有未确认 Stop（REQUESTED/UNCONFIRMED）或 QUARANTINED 资源。

    AB09：对账可采纳原结果，但新 ENGINEERING 写入与最终屏障须关闭。
    """
    open_stops = db.execute(
        text(
            """SELECT 1 FROM stops s
            JOIN activities a ON a.id = s.activity_id
            WHERE a.goal_id = :goal
              AND s.status IN ('REQUESTED', 'UNCONFIRMED')
            LIMIT 1"""
        ),
        {"goal": goal_id},
    ).first()
    if open_stops is not None:
        return True
    quarantined = db.execute(
        text(
            """SELECT 1 FROM resource_reservations r
            JOIN activities a ON a.id = r.activity_id
            WHERE a.goal_id = :goal AND r.status = 'QUARANTINED'
            LIMIT 1"""
        ),
        {"goal": goal_id},
    ).first()
    return quarantined is not None


def goal_has_unconfirmed_control_stop(db: Connection, goal_id: UUID) -> bool:
    """PAUSING/CANCELLING 完成前：仍有 PAUSE/CANCEL 的未确认 Stop。

    不含 LEASE_EXPIRED（失租 Stop 另有隔离/重领路径）；避免无进程杀伤时永久卡死 pause。
    """
    return (
        db.execute(
            text(
                """SELECT 1 FROM stops s
                JOIN activities a ON a.id = s.activity_id
                WHERE a.goal_id = :goal
                  AND s.reason IN ('PAUSE', 'CANCEL')
                  AND s.status IN ('REQUESTED', 'UNCONFIRMED')
                LIMIT 1"""
            ),
            {"goal": goal_id},
        ).first()
        is not None
    )


def assert_goal_allows_new_engineering_writes(db: Connection, goal_id: UUID) -> None:
    """拒绝新 ENGINEERING 副作用咽喉：未确认 Stop/隔离，或 Goal 已 pause/cancel/BLOCKED/终态。"""
    status = db.execute(
        text("SELECT status FROM goals WHERE id=:id"),
        {"id": goal_id},
    ).scalar()
    if status is None:
        raise PlanRejected("Goal 不存在")
    if status in (
        "PAUSING",
        "PAUSED",
        "CANCELLING",
        "CANCELLED",
        "BLOCKED",
        "VERIFYING",
        "DONE",
        "FAILED",
    ):
        raise PlanRejected(
            f"GOAL_ENGINEERING_CLOSED: Goal 处于 {status}，禁止新 ENGINEERING 写入"
        )
    if goal_has_unconfirmed_stop_barrier(db, goal_id):
        raise PlanRejected(
            "STOP_UNCONFIRMED: 存在未确认 Stop 或隔离资源，禁止新 ENGINEERING 写入"
        )
    from .audits import goal_has_blocking_review

    if goal_has_blocking_review(db, goal_id):
        raise PlanRejected(
            "GOAL_REVIEW_BLOCKER: 最新 GoalReview 含 BLOCKER，禁止新 ENGINEERING 写入"
        )
    from .activation_terminations import goal_has_unresolved_no_progress_stop

    if goal_has_unresolved_no_progress_stop(db, goal_id):
        raise PlanRejected(
            "NO_PROGRESS_STOP_UNRESOLVED: 存在未复盘无进展终止，禁止新 ENGINEERING 写入"
        )


def assert_goal_allows_new_inference(db: Connection, goal_id: UUID) -> None:
    """拒绝新推理输入：pause/cancel/BLOCKED/DONE/FAILED（不含 VERIFYING，AUDIT 仍可推理）。"""
    status = db.execute(
        text("SELECT status FROM goals WHERE id=:id"),
        {"id": goal_id},
    ).scalar()
    if status is None:
        raise PlanRejected("Goal 不存在")
    if status in (
        "PAUSING",
        "PAUSED",
        "CANCELLING",
        "CANCELLED",
        "BLOCKED",
        "DONE",
        "FAILED",
    ):
        raise PlanRejected(
            f"GOAL_INFERENCE_CLOSED: Goal 处于 {status}，禁止新推理输入"
        )


def assert_goal_allows_success_outcome(db: Connection, goal_id: UUID) -> None:
    """拒绝成功 outcome 推进业务：BLOCKED/终态（PAUSING 排空仍允许活动收尾）。"""
    status = db.execute(
        text("SELECT status FROM goals WHERE id=:id"),
        {"id": goal_id},
    ).scalar()
    if status is None:
        raise PlanRejected("Goal 不存在")
    if status in ("BLOCKED", "DONE", "FAILED", "CANCELLED"):
        raise PlanRejected(
            f"GOAL_OUTCOME_CLOSED: Goal 处于 {status}，禁止成功 outcome 推进业务"
        )


def _request_from_row(row) -> StopRequest:
    return StopRequest(
        request_id=row["request_id"],
        activation_id=row["activation_id"],
        activity_id=row["activity_id"],
        attempt_id=row["attempt_id"],
        fencing_epoch=str(row["fencing_epoch"]),
        reason=row["reason"],
        deadline_at=row["deadline_at"],
    )


def _resource_from_row(row) -> StopResource:
    return StopResource(
        id=row["id"],
        request=_request_from_row(row),
        status=row["status"],
        state_revision=row["state_revision"],
        receipt_ids=list(row["receipt_ids"] or []),
    )


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _params_match(row, body: StopRequest) -> bool:
    return (
        row["activation_id"] == body.activation_id
        and row["activity_id"] == body.activity_id
        and row["attempt_id"] == body.attempt_id
        and str(row["fencing_epoch"]) == body.fencing_epoch
        and row["reason"] == body.reason
        and _utc(row["deadline_at"]) == _utc(body.deadline_at)
    )


def _require_caller(db, subject: str, *, allow_admin: bool, roles: set[str]) -> None:
    """admin 可代表 Kernel；否则须为已登记 ACTIVE worker。"""
    if allow_admin and "admin" in roles:
        return
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


def insert_stop_request(
    db: Connection, body: StopRequest, project_id: UUID
) -> StopResource:
    """事务内写入 StopRequest：advisory lock + 幂等插入；不含 worker ACL / fencing。

    调用方须已校验 activation_id==attempt_id、时区与（HTTP 路径下的）fencing。
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
        {"scope": f"stop:{body.activation_id}:{body.request_id}"},
    )
    existing = (
        db.execute(
            text(
                """SELECT * FROM stops
                WHERE activation_id=:activation AND request_id=:request
                FOR UPDATE"""
            ),
            {"activation": body.activation_id, "request": body.request_id},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        if not _params_match(existing, body):
            raise PlanRejected("STOP_REQUEST_CONFLICT")
        return _resource_from_row(existing)

    row = (
        db.execute(
            text(
                """INSERT INTO stops(
                  id,project_id,request_id,activation_id,activity_id,attempt_id,
                  fencing_epoch,reason,deadline_at,status,state_revision,receipt_ids)
                VALUES(
                  :id,:project,:request,:activation,:activity,:attempt,
                  :epoch,:reason,:deadline,'REQUESTED',1,'{}')
                RETURNING *"""
            ),
            {
                "id": uuid4(),
                "project": project_id,
                "request": body.request_id,
                "activation": body.activation_id,
                "activity": body.activity_id,
                "attempt": body.attempt_id,
                "epoch": int(body.fencing_epoch),
                "reason": body.reason,
                "deadline": body.deadline_at,
            },
        )
        .mappings()
        .one()
    )
    return _resource_from_row(row)


def request_stop(
    engine: Engine,
    subject: str,
    *,
    roles: set[str],
    project_ids: list[str],
    activation_id: UUID,
    body: StopRequest,
) -> StopResource:
    """记录停止意图，返回 REQUESTED；绝不在无 receipt 时写成 CONFIRMED。"""
    if body.activation_id != activation_id:
        raise PlanRejected("路径 activation_id 与请求体不一致")
    # 本切片：activation_id == attempt_id，禁止另造假映射
    if body.activation_id != body.attempt_id:
        raise PlanRejected("本切片要求 activation_id 等于 attempt_id")
    if body.deadline_at.tzinfo is None:
        raise PlanRejected("deadline_at 必须带时区")

    with engine.begin() as db:
        _require_caller(db, subject, allow_admin=True, roles=roles)
        attempt = (
            db.execute(
                text(
                    """SELECT a.*, act.project_id AS activity_project_id
                    FROM activity_attempts a
                    JOIN activities act ON act.id=a.activity_id
                    WHERE a.id=:id FOR UPDATE OF a"""
                ),
                {"id": body.attempt_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise ScopeNotFound()
        project_id = attempt["project_id"]
        if project_ids and str(project_id) not in project_ids:
            raise ScopeNotFound()
        if attempt["activity_id"] != body.activity_id:
            raise PlanRejected("attempt 与 activity_id 不匹配")

        # 幂等优先：同 activation+request_id 重传在 fencing 前进后仍返回首次 StopResource
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": f"stop:{body.activation_id}:{body.request_id}"},
        )
        existing = (
            db.execute(
                text(
                    """SELECT * FROM stops
                    WHERE activation_id=:activation AND request_id=:request
                    FOR UPDATE"""
                ),
                {"activation": body.activation_id, "request": body.request_id},
            )
            .mappings()
            .first()
        )
        if existing is None and str(attempt["fencing_epoch"]) != body.fencing_epoch:
            raise PlanRejected("fencing_epoch 与当前 attempt 不匹配")
        return insert_stop_request(db, body, project_id)


def _receipt_fields_match(prior, body: StopReceipt, stop_id: UUID) -> bool:
    return (
        prior["stop_id"] == stop_id
        and prior["activation_id"] == body.activation_id
        and prior["attempt_id"] == body.attempt_id
        and prior["resource_instance_id"] == body.resource_instance_id
        and prior["observation"] == body.observation
        and bool(prior["compute_released"]) == body.compute_released
        and bool(prior["write_capability_revoked"]) == body.write_capability_revoked
    )


def apply_stop_receipt(
    engine: Engine,
    subject: str,
    *,
    roles: set[str],
    project_ids: list[str],
    stop_id: UUID,
    body: StopReceipt,
) -> StopReceiptAccepted:
    """写入可信观察；仅 EXITED∧compute_released 可将 StopResource 推至 CONFIRMED。

    权威持有者取自 stops.attempt_id（不得信请求体自报 attempt）。
    身份不符零副作用；失租后原持有者仍可交回执（不要求 attempt ACTIVE）。
    正确持有者提交参数矛盾回执 → PENDING_RECONCILIATION（落库对账，不推进 CONFIRMED）。
    Goal 终态后拒绝新回执（幂等旧回执除外）；与 expire/finalize 同 Goal admission 锁。
    """
    del roles  # 回执不再用 admin 绕过持有者身份
    if body.stop_id != stop_id:
        raise PlanRejected("路径 stop_id 与回执不一致")
    if body.activation_id != body.attempt_id:
        raise PlanRejected("本切片要求 activation_id 等于 attempt_id")
    if body.observed_at.tzinfo is None:
        raise PlanRejected("observed_at 必须带时区")

    with engine.begin() as db:
        from .effects import _require_attempt_holder
        from .goals import acquire_goal_admission_lock

        peek = (
            db.execute(
                text(
                    """SELECT s.*, a.goal_id AS activity_goal_id
                    FROM stops s
                    JOIN activities a ON a.id = s.activity_id
                    WHERE s.id=:id"""
                ),
                {"id": stop_id},
            )
            .mappings()
            .first()
        )
        if peek is None:
            raise ScopeNotFound()
        if project_ids and str(peek["project_id"]) not in project_ids:
            raise ScopeNotFound()

        goal_id = peek["activity_goal_id"]
        if goal_id is not None:
            # 锁序：admission 先于 stops FOR UPDATE（与 expire/finalize 一致，防死锁）
            acquire_goal_admission_lock(db, goal_id)
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal_id},
            ).scalar()
            if goal_status in ("DONE", "FAILED", "CANCELLED"):
                prior = (
                    db.execute(
                        text("SELECT * FROM stop_receipts WHERE receipt_id=:id"),
                        {"id": body.receipt_id},
                    )
                    .mappings()
                    .first()
                )
                if prior is not None:
                    if not _receipt_fields_match(prior, body, stop_id):
                        raise PlanRejected("STOP_RECEIPT_CONFLICT")
                    return StopReceiptAccepted(disposition="DUPLICATE")
                raise PlanRejected(
                    f"GOAL_ENGINEERING_CLOSED: Goal 处于 {goal_status}，拒绝新 Stop 回执"
                )

        # 身份门禁先于任何写入：owner = Stop 登记 attempt，非 body.attempt_id
        _require_attempt_holder(db, subject, peek["attempt_id"])

        stop = (
            db.execute(
                text("SELECT * FROM stops WHERE id=:id FOR UPDATE"),
                {"id": stop_id},
            )
            .mappings()
            .first()
        )
        if stop is None:
            raise ScopeNotFound()

        prior = (
            db.execute(
                text("SELECT * FROM stop_receipts WHERE receipt_id=:id"),
                {"id": body.receipt_id},
            )
            .mappings()
            .first()
        )
        if prior is not None:
            if not _receipt_fields_match(prior, body, stop_id):
                raise PlanRejected("STOP_RECEIPT_CONFLICT")
            return StopReceiptAccepted(disposition="DUPLICATE")

        # 参数矛盾或旧 activation：入库为 PENDING，不推进停止确认
        mismatched = (
            body.activation_id != stop["activation_id"]
            or body.attempt_id != stop["attempt_id"]
        )
        disposition = "PENDING_RECONCILIATION" if mismatched else "APPLIED"

        db.execute(
            text(
                """INSERT INTO stop_receipts(
                  receipt_id,stop_id,project_id,activation_id,attempt_id,
                  resource_instance_id,observed_at,observation,compute_released,
                  write_capability_revoked,proof_artifact_ids,disposition)
                VALUES(
                  :rid,:sid,:project,:activation,:attempt,
                  :resource,:observed,:observation,:compute,
                  :write_revoked,:proofs,:disposition)"""
            ),
            {
                "rid": body.receipt_id,
                "sid": stop_id,
                "project": stop["project_id"],
                "activation": body.activation_id,
                "attempt": body.attempt_id,
                "resource": body.resource_instance_id,
                "observed": body.observed_at,
                "observation": body.observation,
                "compute": body.compute_released,
                "write_revoked": body.write_capability_revoked,
                "proofs": body.proof_artifact_ids,
                "disposition": disposition,
            },
        )

        if mismatched:
            return StopReceiptAccepted(disposition="PENDING_RECONCILIATION")

        receipt_ids = list(stop["receipt_ids"] or []) + [body.receipt_id]
        new_status = stop["status"]
        # 单实例切片：CONFIRMED 当且仅当 EXITED 且 compute_released
        if body.observation == "EXITED" and body.compute_released:
            new_status = "CONFIRMED"
            # 停机已确认：释放该 attempt 上隔离或仍占用的资源/预算
            db.execute(
                text("""UPDATE resource_reservations
                  SET status='RELEASED', updated_at=clock_timestamp()
                  WHERE attempt_id=:id AND status IN ('HELD','QUARANTINED')"""),
                {"id": stop["attempt_id"]},
            )
            db.execute(
                text("""UPDATE budget_reservations
                  SET status='RELEASED', updated_at=clock_timestamp()
                  WHERE attempt_id=:id AND status IN ('HELD','QUARANTINED')"""),
                {"id": stop["attempt_id"]},
            )
        # ISOLATED / RUNNING / UNKNOWN / EXITED 未释放计算：保持原状态（不伪 CONFIRMED）
        # 已 CONFIRMED 不再因后续 RUNNING 回退；亦不得在未 CONFIRMED 时释放隔离槽

        if new_status != stop["status"] or receipt_ids != list(stop["receipt_ids"] or []):
            db.execute(
                text(
                    """UPDATE stops SET status=:status, receipt_ids=:receipts,
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                    WHERE id=:id"""
                ),
                {
                    "id": stop_id,
                    "status": new_status,
                    "receipts": receipt_ids,
                },
            )
        # 必须先落库 CONFIRMED，再泊活动 / reassess / 完成 pause-cancel
        if new_status == "CONFIRMED":
            from .claims import reassess_recovering_activity
            from .control_commands import maybe_complete_pause_or_cancel
            from .finalization import try_seal_draining_barrier

            _park_activity_after_control_stop_confirmed(db, stop)
            reassess_recovering_activity(db, stop["activity_id"])
            parked_goal_id = db.execute(
                text("SELECT goal_id FROM activities WHERE id=:id"),
                {"id": stop["activity_id"]},
            ).scalar()
            if parked_goal_id is not None:
                maybe_complete_pause_or_cancel(db, parked_goal_id)
                # AB09 / doc/01 §9：Stop 确认后尝试 DRAINING→SEALED（≠DONE）
                try_seal_draining_barrier(db, parked_goal_id)
        return StopReceiptAccepted(disposition="APPLIED")


def _park_activity_after_control_stop_confirmed(db: Connection, stop) -> None:
    """控制类 Stop CONFIRMED：结束 ACTIVE attempt，并泊入 WAITING 或 CANCELLED。

    - PAUSE → WAITING(PAUSED)（可 resume）
    - CANCEL / SHUTDOWN / TRUST_INVALIDATION → CANCELLED（排空终态，≠ Goal DONE）
    doc/01：不杀进程；放弃/信任排空确认后不得残留 RUNNING 挡人工解除。
    """
    reason = stop["reason"]
    if reason not in ("PAUSE", "CANCEL", "SHUTDOWN", "TRUST_INVALIDATION"):
        return
    now = datetime.now(UTC)
    db.execute(
        text(
            """UPDATE activity_attempts SET status='CANCELLED', finished_at=:now,
              updated_at=clock_timestamp()
            WHERE id=:id AND status='ACTIVE'"""
        ),
        {"id": stop["attempt_id"], "now": now},
    )
    if reason == "PAUSE":
        deadline = now + timedelta(hours=24)
        db.execute(
            text(
                """UPDATE activities SET status='WAITING',
                  wait_reason='PAUSED', resume_state='READY',
                  wake_at=NULL, wait_deadline_at=:deadline,
                  current_attempt_id=NULL, updated_at=clock_timestamp()
                WHERE id=:id AND status='RUNNING'"""
            ),
            {"id": stop["activity_id"], "deadline": deadline},
        )
    else:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED',
                  wait_reason=NULL, resume_state=NULL,
                  wake_at=NULL, wait_deadline_at=NULL,
                  current_attempt_id=NULL, updated_at=clock_timestamp()
                WHERE id=:id AND status='RUNNING'"""
            ),
            {"id": stop["activity_id"]},
        )


def get_stop(
    engine: Engine,
    subject: str,
    *,
    roles: set[str],
    project_ids: list[str],
    stop_id: UUID,
) -> StopResource:
    """读取 StopResource；过 deadline 仍无 CONFIRMED 时标为 UNCONFIRMED。"""
    with engine.begin() as db:
        _require_caller(db, subject, allow_admin=True, roles=roles)
        row = (
            db.execute(
                text("SELECT * FROM stops WHERE id=:id FOR UPDATE"),
                {"id": stop_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        if project_ids and str(row["project_id"]) not in project_ids:
            raise ScopeNotFound()

        if row["status"] == "REQUESTED":
            now = datetime.now(UTC)
            deadline = row["deadline_at"]
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if now >= deadline.astimezone(UTC):
                row = (
                    db.execute(
                        text(
                            """UPDATE stops SET status='UNCONFIRMED',
                              state_revision=state_revision+1,
                              updated_at=clock_timestamp()
                            WHERE id=:id AND status='REQUESTED'
                            RETURNING *"""
                        ),
                        {"id": stop_id},
                    )
                    .mappings()
                    .one()
                )
        return _resource_from_row(row)
