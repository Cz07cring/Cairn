"""Temporal 编排绑定 / 投递 / 具名 admit；DB 权威在 Kernel，无 Temporal SDK。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from ..domain.handoff_envelope import HandoffRejected, assert_execute_admission_handoff
from ..protocols.runtime import (
    ActivityLease,
    BindingStale,
    ExecutionBinding,
    LeaseIdentity,
    LeaseRejected,
    PlanRejected,
    RuntimeActionRef,
    RuntimeActionsResult,
    RuntimeWaitHint,
    WorkerForbidden,
)
from .activities import _activity_from_row
from .claims import (
    DEFAULT_LEASE_TTL,
    _attempt_from_row,
    _live_binding,
    _worker_usage,
    binding_digest_of,
    expire_stale_leases,
)
from .plan_inputs import resolve_required_plan_input
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict

# 投递事件种类；与 doc/v0.6/05 §4 UNIQUE(command_id,event_kind) 对齐
EVENT_ENSURE_WORKFLOW = "ENSURE_WORKFLOW"
DEFAULT_TEMPORAL_NAMESPACE = "default"
DEFAULT_WORKER_BUILD_ID = "m0-py-1.32.0-dev"

# pause/cancel 排空与终态：禁止新 ENGINEERING 准入（对齐全局 claim 的 Goal 状态门）
_CONTROL_DRAIN_STATUSES = frozenset({"PAUSING", "PAUSED", "CANCELLING", "CANCELLED"})
_ENGINEERING_ADMIT_KINDS = frozenset({"PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE"})


def _assert_admit_allowed_for_goal_status(
    kind: str, goal_status: str, *, target_type: str | None = None
) -> None:
    """具名 admit 的 Goal 状态门：与 LEGACY claim 同口径，并显式拒绝 pause/cancel 排空态。"""
    if goal_status == "BLOCKED" and kind in _ENGINEERING_ADMIT_KINDS:
        raise LeaseRejected(
            "INVALID_STATE",
            f"Goal 处于 BLOCKED，禁止准入 {kind}",
        )
    if kind in _ENGINEERING_ADMIT_KINDS and goal_status in _CONTROL_DRAIN_STATUSES:
        raise LeaseRejected(
            "INVALID_STATE",
            f"Goal 处于 {goal_status}，禁止准入 {kind}",
        )
    if kind == "PLAN" and goal_status != "PLANNING":
        raise LeaseRejected("INVALID_STATE", "Goal 非 PLANNING，无法准入 PLAN")
    if kind == "EXECUTE" and goal_status != "RUNNING":
        raise LeaseRejected("INVALID_STATE", "Goal 非 RUNNING，无法准入 EXECUTE")
    if kind == "AUDIT" and target_type == "GOAL_REVIEW":
        if goal_status not in ("PLANNING", "RUNNING", "VERIFYING"):
            raise LeaseRejected(
                "INVALID_STATE",
                "Goal 非 PLANNING/RUNNING/VERIFYING，无法准入 GOAL_REVIEW",
            )
    elif kind == "AUDIT" and goal_status not in ("RUNNING", "VERIFYING"):
        raise LeaseRejected("INVALID_STATE", "Goal 非 RUNNING/VERIFYING，无法准入 AUDIT")
    if kind == "FINALIZE" and goal_status != "VERIFYING":
        raise LeaseRejected("INVALID_STATE", "Goal 非 VERIFYING，无法准入 FINALIZE")
    if kind == "INTEGRATE" and goal_status != "RUNNING":
        raise LeaseRejected("INVALID_STATE", "Goal 非 RUNNING，无法准入 INTEGRATE")
    if kind == "RECONCILE" and goal_status in ("DONE", "CANCELLED", "FAILED"):
        raise LeaseRejected("INVALID_STATE", f"Goal 已终态 {goal_status}，无法准入 RECONCILE")


def workflow_id_for_goal(goal_id: UUID) -> str:
    return f"goal-{goal_id}"


def upsert_binding_for_temporal_start(
    db,
    goal,
    command_id: UUID,
    *,
    namespace: str = DEFAULT_TEMPORAL_NAMESPACE,
    worker_build_id: str = DEFAULT_WORKER_BUILD_ID,
) -> dict:
    """START 同事务：若尚无绑定则插入 TEMPORAL orchestration_bindings。

    command_id 仅作审计关联；绑定以 goal_id 唯一。
    """
    existing = (
        db.execute(
            text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal FOR UPDATE"),
            {"goal": goal["id"]},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return dict(existing)

    workflow_id = workflow_id_for_goal(goal["id"])
    row = (
        db.execute(
            text(
                """INSERT INTO orchestration_bindings(
                  id,project_id,goal_id,budget_scope_id,backend,owner_epoch,
                  namespace,workflow_id,active_run_id,worker_build_id,contract_digest)
                VALUES(
                  :id,:project,:goal,:budget,'TEMPORAL',:epoch,
                  :namespace,:workflow,NULL,:build,:digest)
                RETURNING *"""
            ),
            {
                "id": uuid4(),
                "project": goal["project_id"],
                "goal": goal["id"],
                "budget": goal["id"],
                "epoch": goal["owner_epoch"],
                "namespace": namespace,
                "workflow": workflow_id,
                "build": worker_build_id,
                "digest": goal["contract_digest"],
            },
        )
        .mappings()
        .one()
    )
    return dict(row)


def enqueue_ensure_workflow(
    db,
    *,
    project_id: UUID,
    goal_id: UUID,
    command_id: UUID,
    workflow_id: str,
) -> dict:
    """写入 PENDING ENSURE_WORKFLOW 投递；UNIQUE(command_id,event_kind) 幂等。"""
    existing = (
        db.execute(
            text(
                """SELECT * FROM orchestration_deliveries
                WHERE command_id=:command AND event_kind=:kind FOR UPDATE"""
            ),
            {"command": command_id, "kind": EVENT_ENSURE_WORKFLOW},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return dict(existing)

    row = (
        db.execute(
            text(
                """INSERT INTO orchestration_deliveries(
                  id,project_id,goal_id,command_id,event_kind,workflow_id,
                  run_id,delivery_status)
                VALUES(
                  :id,:project,:goal,:command,:kind,:workflow,NULL,'PENDING')
                RETURNING *"""
            ),
            {
                "id": uuid4(),
                "project": project_id,
                "goal": goal_id,
                "command": command_id,
                "kind": EVENT_ENSURE_WORKFLOW,
                "workflow": workflow_id,
            },
        )
        .mappings()
        .one()
    )
    return dict(row)


def get_delivery_for_command(
    db, command_id: UUID, event_kind: str = EVENT_ENSURE_WORKFLOW
) -> dict | None:
    row = (
        db.execute(
            text(
                """SELECT * FROM orchestration_deliveries
                WHERE command_id=:command AND event_kind=:kind"""
            ),
            {"command": command_id, "kind": event_kind},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def get_binding_for_goal(db, goal_id: UUID) -> dict | None:
    row = (
        db.execute(
            text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
            {"goal": goal_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def acknowledge_delivery(
    db,
    *,
    command_id: UUID,
    run_id: str,
    event_kind: str = EVENT_ENSURE_WORKFLOW,
) -> dict:
    """将 PENDING 投递标为 ACKNOWLEDGED，回填 binding.active_run_id，命令 ACCEPTED→SUCCEEDED。

    已 ACK 时幂等返回原行。不可用时由调用方保持 PENDING，本函数不回退 LEGACY。
    """
    delivery = (
        db.execute(
            text(
                """SELECT * FROM orchestration_deliveries
                WHERE command_id=:command AND event_kind=:kind FOR UPDATE"""
            ),
            {"command": command_id, "kind": event_kind},
        )
        .mappings()
        .first()
    )
    if delivery is None:
        raise ScopeNotFound()
    if delivery["delivery_status"] == "ACKNOWLEDGED":
        return dict(delivery)

    updated = (
        db.execute(
            text(
                """UPDATE orchestration_deliveries
                SET delivery_status='ACKNOWLEDGED', run_id=:run_id,
                    updated_at=clock_timestamp()
                WHERE id=:id AND delivery_status='PENDING'
                RETURNING *"""
            ),
            {"id": delivery["id"], "run_id": run_id},
        )
        .mappings()
        .first()
    )
    if updated is None:
        # 并发下已被 ACK
        again = get_delivery_for_command(db, command_id, event_kind)
        if again is None:
            raise ScopeNotFound()
        return again

    db.execute(
        text(
            """UPDATE orchestration_bindings
            SET active_run_id=:run_id, updated_at=clock_timestamp()
            WHERE goal_id=:goal"""
        ),
        {"run_id": run_id, "goal": delivery["goal_id"]},
    )
    db.execute(
        text(
            """UPDATE command_operations
            SET status='SUCCEEDED', updated_at=clock_timestamp()
            WHERE id=:id AND status='ACCEPTED'"""
        ),
        {"id": command_id},
    )
    return dict(updated)


def get_runtime_actions(
    engine,
    subject: str,
    *,
    goal_id: UUID,
    expected_owner_epoch: str,
    project_ids: list[str],
) -> RuntimeActionsResult:
    """返回 TEMPORAL Goal 下 READY 活动的 RuntimeActionRef；Kernel 只计算，不启动 Runner。

    无 READY 时 actions 为空并附 wait_hint。拒绝非 TEMPORAL / owner_epoch 不匹配。
    Goal 处于 pause/cancel 排空态时不下发 ENGINEERING 动作（admit 已拒绝，此处防空转）。
    """
    with engine.connect() as db:
        goal = (
            db.execute(
                text(
                    """SELECT id, project_id, status, orchestration_backend, owner_epoch,
                       plan_input_mode, contract_revision, contract_digest, plan_revision
                    FROM goals WHERE id=:id"""
                ),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if goal is None:
            raise ScopeNotFound()
        # 项目成员 / JWT project_ids，或已登记 ACTIVE worker（编排拉取，不依赖 project_ids）
        try:
            ConfigurationVersions.check_scope(db, goal["project_id"], subject, project_ids)
        except ScopeNotFound:
            worker = (
                db.execute(
                    text("SELECT id FROM workers WHERE subject=:subject AND status='ACTIVE'"),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker is None:
                raise ScopeNotFound()
        if goal["orchestration_backend"] != "TEMPORAL":
            raise LeaseRejected(
                "INVALID_STATE",
                "仅 TEMPORAL Goal 可查询 runtime actions",
            )
        if str(goal["owner_epoch"]) != str(expected_owner_epoch):
            raise LeaseRejected(
                "OWNER_EPOCH_MISMATCH",
                "owner_epoch 与期望不一致",
            )

        binding = get_binding_for_goal(db, goal_id)
        if binding is None:
            raise ScopeNotFound()
        if binding["backend"] != "TEMPORAL":
            raise LeaseRejected(
                "INVALID_STATE",
                "编排绑定不是 TEMPORAL，禁止查询 runtime actions",
            )
        if str(binding["owner_epoch"]) != str(expected_owner_epoch):
            raise LeaseRejected(
                "OWNER_EPOCH_MISMATCH",
                "绑定 owner_epoch 与期望不一致",
            )

        rows = (
            db.execute(
                text(
                    """SELECT id, project_id, goal_id, kind, binding
                    FROM activities
                    WHERE goal_id=:goal AND status='READY'
                    ORDER BY created_at ASC, id ASC"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .all()
        )

        waiting_for_input = False
        if goal["plan_input_mode"] == "REQUIRED" and any(r["kind"] == "PLAN" for r in rows):
            try:
                resolve_required_plan_input(db, goal)
            except (PlanRejected, ValueError, RuntimeError):
                waiting_for_input = True

    draining = goal["status"] in _CONTROL_DRAIN_STATUSES
    if draining:
        rows = [row for row in rows if row["kind"] not in _ENGINEERING_ADMIT_KINDS]
    if waiting_for_input:
        rows = [row for row in rows if row["kind"] != "PLAN"]

    actions = [
        RuntimeActionRef(
            project_id=row["project_id"],
            goal_id=row["goal_id"],
            activity_id=row["id"],
            action_id=row["id"],
            owner_epoch=str(goal["owner_epoch"]),
            binding_digest=binding_digest_of(row["binding"]),
        )
        for row in rows
    ]
    wait_hint = None
    if draining and not actions:
        wait_hint = RuntimeWaitHint(
            code="GOAL_CONTROL_DRAINING",
            message=f"Goal 处于 {goal['status']}，暂不下发 ENGINEERING 动作",
        )
    elif waiting_for_input and not draining:
        wait_hint = RuntimeWaitHint(
            code="PLAN_INPUT_REQUIRED",
            message="PLAN 等待唯一有效的 STAGED PlanInput；未准入 attempt",
        )
    elif not actions:
        wait_hint = RuntimeWaitHint(
            code="NO_READY_ACTIVITIES",
            message="当前无 READY 活动，保持等待",
        )
    return RuntimeActionsResult(actions=actions, wait_hint=wait_hint)


def admit_runtime_attempt(
    engine,
    subject: str,
    key: str,
    activity_id: UUID,
    *,
    lease_ttl: timedelta = DEFAULT_LEASE_TTL,
) -> ActivityLease:
    """具名准入 TEMPORAL Goal 的 READY Activity；拒绝 LEGACY / 信任阻塞 / 后端不匹配。"""
    path = "/internal/v1/runtime/admit"
    request_scope = json.dumps(
        [subject, "POST", path, key, str(activity_id)], separators=(",", ":")
    )
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"activity_id": str(activity_id)}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
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
            return ActivityLease.model_validate(old["result"])

        worker = (
            db.execute(
                text("SELECT * FROM workers WHERE subject=:subject AND status='ACTIVE' FOR UPDATE"),
                {"subject": subject},
            )
            .mappings()
            .first()
        )
        if worker is None:
            raise WorkerForbidden()

        expire_stale_leases(db)

        row = (
            db.execute(
                text("SELECT * FROM activities WHERE id=:id FOR UPDATE"),
                {"id": activity_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        if row["status"] != "READY":
            raise LeaseRejected("INVALID_STATE", f"活动状态为 {row['status']}，无法准入")
        if row["kind"] not in set(worker["allowed_kinds"] or []):
            raise WorkerForbidden()

        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": row["project_id"]},
        ).scalar_one()
        if trust != "OPEN":
            raise TrustBlocked()

        if row["goal_id"] is None:
            raise LeaseRejected("INVALID_STATE", "无 Goal 活动请走全局 claim")
        goal_row = (
            db.execute(
                text(
                    """SELECT *
                    FROM goals WHERE id=:id FOR UPDATE"""
                ),
                {"id": row["goal_id"]},
            )
            .mappings()
            .one()
        )
        if goal_row["orchestration_backend"] != "TEMPORAL":
            raise LeaseRejected(
                "INVALID_STATE",
                "LEGACY Goal 须经全局 claim，禁止 admit_runtime_attempt",
            )
        _assert_admit_allowed_for_goal_status(
            row["kind"], goal_row["status"], target_type=row.get("target_type")
        )
        selected_input = None
        if row["kind"] == "PLAN" and goal_row["plan_input_mode"] == "REQUIRED":
            try:
                selected_input = resolve_required_plan_input(db, goal_row)
            except (PlanRejected, ValueError, RuntimeError) as exc:
                raise LeaseRejected("PLAN_INPUT_REQUIRED", str(exc)) from exc

        # Issue #20：ENGINEERING 准入前推进墙钟；耗尽/UNKNOWN 失败关闭
        if row["kind"] in ("PLAN", "EXECUTE", "INTEGRATE", "AUDIT", "FINALIZE"):
            from .budget_clock import assert_goal_wall_budget_allows_engineering

            assert_goal_wall_budget_allows_engineering(db, row["goal_id"])

        scope_lock = json.dumps(
            ["budget", str(row["project_id"]), str(row["budget_scope_id"])],
            separators=(",", ":"),
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": scope_lock},
        )
        live_binding_raw = _live_binding(db, row)
        if binding_digest_of(row["binding"]) != binding_digest_of(live_binding_raw):
            raise BindingStale()

        # M4：EXECUTE 准入前 HandoffEnvelope ACK（UNKNOWN 未对账 / 绑定漂移均拒）
        if row["kind"] == "EXECUTE":
            unknown_ids = list(
                db.execute(
                    text(
                        """SELECT id FROM effect_intents
                        WHERE goal_id=:goal AND status='UNKNOWN'"""
                    ),
                    {"goal": row["goal_id"]},
                ).scalars()
            )
            try:
                assert_execute_admission_handoff(
                    goal_id=row["goal_id"],
                    task_id=row["task_id"],
                    binding=ExecutionBinding.model_validate(row["binding"]),
                    live_binding=ExecutionBinding.model_validate(live_binding_raw),
                    live_plan_revision=goal_row["plan_revision"],
                    unknown_effect_ids=unknown_ids,
                    receiver_activity_id=activity_id,
                )
            except HandoffRejected as err:
                raise LeaseRejected(err.code, err.message) from err

        need = row["resources"]
        cpu, mem, disk, model, browser = _worker_usage(db, worker["id"])
        if (
            cpu + need["cpu_millicores"] > worker["cpu_millicores"]
            or mem + need["memory_bytes"] > worker["memory_bytes"]
            or disk + need["disk_bytes"] > worker["disk_bytes"]
            or model + need["model_slots"] > worker["model_slots"]
            or browser + need["browser_slots"] > worker["browser_slots"]
        ):
            raise LeaseRejected("RESOURCE_EXHAUSTED", "Worker 资源不足以准入该活动")

        epoch = db.execute(
            text(
                "SELECT COALESCE(MAX(fencing_epoch),0)+1 FROM activity_attempts WHERE activity_id=:id"
            ),
            {"id": row["id"]},
        ).scalar_one()
        now = datetime.now(UTC)
        expires = now + lease_ttl
        attempt_id = uuid4()
        attempt_row = (
            db.execute(
                text(
                    """INSERT INTO activity_attempts(
                  id,activity_id,project_id,worker_id,binding_digest,fencing_epoch,
                  lease_expires_at,renewal_seq,status,skill_versions,started_at,
                  plan_input_id,plan_input_digest)
                VALUES(
                  :id,:activity,:project,:worker,:binding,:epoch,
                  :expires,0,'ACTIVE','[]'::jsonb,:started,:plan_input_id,:plan_input_digest)
                RETURNING *"""
                ),
                {
                    "id": attempt_id,
                    "activity": row["id"],
                    "project": row["project_id"],
                    "worker": worker["id"],
                    "binding": binding_digest_of(row["binding"]),
                    "epoch": epoch,
                    "expires": expires,
                    "started": now,
                    "plan_input_id": selected_input.id if selected_input else None,
                    "plan_input_digest": selected_input.content_digest if selected_input else None,
                },
            )
            .mappings()
            .one()
        )
        updated = (
            db.execute(
                text(
                    """UPDATE activities SET status='RUNNING',
                  state_revision=state_revision+1,
                  current_attempt_id=:attempt,
                  updated_at=clock_timestamp()
                  WHERE id=:id AND status='READY'
                  RETURNING *"""
                ),
                {"id": row["id"], "attempt": attempt_id},
            )
            .mappings()
            .first()
        )
        if updated is None:
            raise LeaseRejected("INVALID_STATE", "活动已被其他 worker 领取")

        from .obligations import open_obligations_for_claim

        open_obligations_for_claim(db, updated, attempt_id)

        db.execute(
            text(
                """INSERT INTO resource_reservations(
              id,project_id,activity_id,attempt_id,worker_id,resources,status)
            VALUES(:id,:project,:activity,:attempt,:worker,CAST(:resources AS jsonb),'HELD')"""
            ),
            {
                "id": uuid4(),
                "project": row["project_id"],
                "activity": row["id"],
                "attempt": attempt_id,
                "worker": worker["id"],
                "resources": json.dumps(row["resources"]),
            },
        )
        db.execute(
            text(
                """INSERT INTO budget_reservations(
              id,project_id,budget_scope_id,activity_id,attempt_id,status)
            VALUES(:id,:project,:scope,:activity,:attempt,'HELD')"""
            ),
            {
                "id": uuid4(),
                "project": row["project_id"],
                "scope": row["budget_scope_id"],
                "activity": row["id"],
                "attempt": attempt_id,
            },
        )
        policy_snapshot_id = UUID(
            db.execute(
                text("SELECT contract->>'policy_id' FROM goals WHERE id=:id"),
                {"id": row["goal_id"]},
            ).scalar_one()
        )
        activity = _activity_from_row(updated)
        attempt = _attempt_from_row(attempt_row)
        result = ActivityLease(
            lease=LeaseIdentity(
                activity_id=activity.id,
                attempt_id=attempt.id,
                fencing_epoch=str(epoch),
            ),
            activity=activity,
            attempt=attempt,
            input_artifact_ids=[],
            policy_snapshot_id=policy_snapshot_id,
            workspace_root=None,
        )
        db.execute(
            text(
                "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
            ),
            {
                "scope": request_scope,
                "digest": request_digest,
                "result": result.model_dump_json(),
            },
        )
        return result
