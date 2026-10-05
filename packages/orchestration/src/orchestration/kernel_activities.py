"""GoalWorkflow 可调用的 Kernel 控制 Activities（允许 IO）。

经 control_kernel / relay 访问业务库与 Temporal 客户端；**禁止**将 Goal 标为 DONE，
**禁止**模型/工具调用。TemporalUnavailable 时 ensure 保持 PENDING，不回退 LEGACY。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from control_kernel.protocols.runtime import PlanRejected
from control_kernel.storage.abandonments import record_orchestration_abandonment
from control_kernel.storage.audits import (
    ensure_goal_review_activity,
    read_goal_review_workflow_start_knobs,
)
from control_kernel.storage.orchestration import (
    admit_runtime_attempt,
    get_binding_for_goal,
    get_runtime_actions,
)
from sqlalchemy import Engine, create_engine, text
from temporalio import activity

from .client import TemporalClient, build_temporal_client
from .protocols import OrchestrationBindingContent
from .relay import ensure_workflow

# 测试可注入；生产由环境变量构造
_engine_override: Engine | None = None
_client_override: TemporalClient | None = None


def configure_kernel_activity_ports(
    *,
    engine: Engine | None = None,
    temporal_client: TemporalClient | None = None,
) -> None:
    """单测注入 Engine / TemporalClient；传 None 清除覆盖。"""
    global _engine_override, _client_override
    _engine_override = engine
    _client_override = temporal_client


def _database_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    url = (env.get("RING_DATABASE_URL") or env.get("RING_TEST_DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "Kernel Activity 需要 RING_DATABASE_URL（或测试用 RING_TEST_DATABASE_URL）"
        )
    return url


def _resolve_engine() -> Engine:
    if _engine_override is not None:
        return _engine_override
    return create_engine(_database_url())


def _resolve_client() -> TemporalClient:
    if _client_override is not None:
        return _client_override
    return build_temporal_client()


def _worker_subject(environ: Mapping[str, str] | None = None) -> str:
    """Worker 身份：RING_WORKFLOW_WORKER_SUBJECT，否则退回服务主体名。"""
    env = os.environ if environ is None else environ
    subject = (env.get("RING_WORKFLOW_WORKER_SUBJECT") or "").strip()
    if subject:
        return subject
    # 无显式 subject 时使用固定服务身份（须已在 workers 表登记）
    return (env.get("RING_SERVICE_SUBJECT") or "ring-workflow-worker").strip()


def _binding_content(row: dict[str, Any]) -> OrchestrationBindingContent:
    return OrchestrationBindingContent(
        project_id=row["project_id"],
        goal_id=row["goal_id"],
        budget_scope_id=row["budget_scope_id"],
        backend=row["backend"],
        owner_epoch=str(row["owner_epoch"]),
        namespace=row["namespace"],
        workflow_id=row["workflow_id"],
        active_run_id=row["active_run_id"],
        worker_build_id=row["worker_build_id"],
        contract_digest=row["contract_digest"],
    )


def _as_nonnegative_int(raw: object) -> int | None:
    """jsonb `->>` 取出为 str/int/None；非严格非负整数一律视为不可用（None）。"""
    if raw is None or isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, str)):
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _read_wall_budget(engine: Engine, goal_id: UUID) -> tuple[int | None, bool]:
    """编排侧**只读**第二道门：返回 (剩余墙钟秒, 是否耗尽或不可信)。

    **写入方是 Kernel**（`storage/budget_clock`，归 Cursor 的批次：`init_budget_clock_on_start`
    / `advance_goal_budget_clock`，并已在 `claims.py` 认领路径与 `orchestration.py` 准入路径
    用 `assert_goal_wall_budget_allows_engineering` 做**权威事务内拒绝**）。本层**只读不写** ——
    编排消费预算是为了在发起 admit 前多一道门（Codex 复核 P1-3 的"第二道门"），不是当写入方。

    语义与 Kernel 的 `goal_wall_budget_snapshot` 对齐：**读数不可解析 → 不可用且失败关闭**
    （返回 `(None, True)`，调用方不得准入），不得把未知当作"消耗 0"（那是失败开放）。

    TODO(收敛)：Kernel 的 `goal_wall_budget_snapshot` 入库后改为直接调用它，
    以消除本层对 jsonb 形状的了解（保持单一解释来源）。
    """
    with engine.connect() as db:
        row = (
            db.execute(
                text(
                    """SELECT (contract->'budget'->>'wall_clock_seconds') AS wall_clock,
                              (budget_usage->>'elapsed_wall_seconds') AS elapsed
                         FROM goals WHERE id=:id"""
                ),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        return None, True
    return _interpret_wall_budget(row["wall_clock"], row["elapsed"])


def _interpret_wall_budget(
    wall_clock_raw: object, elapsed_raw: object
) -> tuple[int | None, bool]:
    """把两列原始读数解释为 (剩余秒, 是否耗尽或不可信) —— 纯函数，便于单测。

    - `wall_clock` 不可读/非正 → 不可用且失败关闭（返回 `(None, True)`）；
    - `elapsed` 缺失 → Kernel 尚未计量（合法），按 0 消耗；
    - `elapsed` 存在但不可读 → 不可用且失败关闭（**不得**当 0）；
    - 全程整数减法，无日期运算 → 合同极大 `wall_clock` 不会溢出。
    """
    wall_clock = _as_nonnegative_int(wall_clock_raw)
    if wall_clock is None or wall_clock <= 0:
        return None, True
    if elapsed_raw is None:
        elapsed = 0
    else:
        parsed = _as_nonnegative_int(elapsed_raw)
        if parsed is None:
            return None, True
        elapsed = parsed
    remaining = wall_clock - elapsed
    return max(0, remaining), remaining <= 0


@activity.defn(name="ensure_goal_delivery")
def ensure_goal_delivery(command_id: str, goal_id: str) -> dict[str, Any]:
    """幂等 ensure_workflow + ACK；返回投递摘要（仅 id / 状态字段与预算摘要）。

    另返回 Goal 的**权威剩余墙钟预算**与读数状态：
      `budget_remaining_wall_seconds` / `budget_exhausted` / `budget_status`（OK|UNKNOWN）

    **权威来自 Kernel 预算账**（写入方：`storage/budget_clock`，Cursor 批次），
    **不是 `created_at`** —— Goal 可长期停留 DRAFT，用创建时刻会把刚启动的
    Goal 判成已过期（Codex 复核 P1-1）。该读取点同时是 `elapsed_wall_seconds`
    的**唯一推进点**（此前全仓无写入方，字段恒为 0；Codex P1-1 前半）。

    **`budget_status="UNKNOWN"` 必须失败关闭**：读数缺失/非法时不得当作
    "消耗为 0"（那等于授予足额窗口，是失败开放）。Workflow 侧见该值即放弃。

    老历史（无这三个字段）→ Workflow 逐字退回旧取值与旧命令序列，重放安全。

    Critic 周期旋钮：从 Goal 合同派生 ``goal_review_interval_seconds`` /
    ``max_goal_reviews`` 并透传 ``ensure_workflow``（与 Kernel 预算快照同源）。
    缺合同派生则 Workflow 侧保持关闭（失败关闭）；禁止仅靠「有实现无写入方」。
    """
    engine = _resolve_engine()
    client = _resolve_client()
    gid = UUID(goal_id)
    cid = UUID(command_id)
    with engine.connect() as db:
        row = get_binding_for_goal(db, gid)
    if row is None:
        raise LookupError(f"编排绑定不存在: goal_id={goal_id}")
    binding = _binding_content(dict(row))
    # Critic 周期：把合同派生的 interval/max 写入启动载荷（否则仅靠 env，合同不可达）
    review_knobs = read_goal_review_workflow_start_knobs(engine, gid)
    receipt = ensure_workflow(engine, binding, cid, client, **review_knobs)

    remaining, exhausted = _read_wall_budget(engine, gid)
    return {
        "command_id": str(receipt.command_id),
        "workflow_id": receipt.workflow_id,
        "run_id": receipt.run_id,
        "delivery_status": receipt.delivery_status,
        # 读数不可用（None）→ UNKNOWN，Workflow 侧必须失败关闭
        "budget_status": "UNKNOWN" if remaining is None else "OK",
        "budget_remaining_wall_seconds": remaining,
        "budget_exhausted": exhausted,
    }


@activity.defn(name="list_runtime_actions")
def list_runtime_actions(goal_id: str, owner_epoch: str) -> dict[str, Any]:
    """包装 get_runtime_actions；附加 kind 供 Workflow 过滤 PLAN。"""
    engine = _resolve_engine()
    subject = _worker_subject()
    gid = UUID(goal_id)
    result = get_runtime_actions(
        engine,
        subject,
        goal_id=gid,
        expected_owner_epoch=owner_epoch,
        project_ids=[],
    )
    actions_out: list[dict[str, str | None]] = []
    with engine.connect() as db:
        for ref in result.actions:
            kind = db.execute(
                text("SELECT kind FROM activities WHERE id=:id"),
                {"id": ref.activity_id},
            ).scalar_one_or_none()
            actions_out.append(
                {
                    "activity_id": str(ref.activity_id),
                    "action_id": str(ref.action_id),
                    "goal_id": str(ref.goal_id) if ref.goal_id else None,
                    "project_id": str(ref.project_id),
                    "owner_epoch": ref.owner_epoch,
                    "kind": kind,
                }
            )
    wait_hint = None
    if result.wait_hint is not None:
        wait_hint = {"code": result.wait_hint.code, "message": result.wait_hint.message}
    return {"actions": actions_out, "wait_hint": wait_hint}


@activity.defn(name="admit_plan_action")
def admit_plan_action(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    """仅准入 PLAN；返回租约摘要 ids。非 PLAN 拒绝。

    Temporal RunActivation（Cordis/Qwen）可能远超默认 90s claim TTL；
    此处使用更长 TTL，并由 Runner heartbeat 续约，避免中途被全局 expire 清掉。
    """
    return _admit_kind_action(activity_id, idempotency_key, expected_kind="PLAN")


@activity.defn(name="admit_execute_action")
def admit_execute_action(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    """仅准入 EXECUTE；返回租约摘要 ids。非 EXECUTE 拒绝。

    与 admit_plan_action 同 TTL/心跳约定；工具副作用仍须经 Broker，本 Activity 不执行工具。
    """
    return _admit_kind_action(activity_id, idempotency_key, expected_kind="EXECUTE")


@activity.defn(name="admit_runtime_action")
def admit_runtime_action(
    activity_id: str, idempotency_key: str, kind: str
) -> dict[str, str | None]:
    """通用准入（AUDIT / INTEGRATE / FINALIZE）；返回租约摘要 ids。

    **为什么需要**：编排工作流原先只准入 PLAN 与 EXECUTE，Kernel 在 EXECUTE 成功后
    自动创建的 AUDIT / INTEGRATE / FINALIZE **无人准入** ⇒ 目标永远到不了 DONE
    （2026-09-15 实测：活动停在 `AUDIT:READY`）。Kernel 的 `admit_runtime_attempt`
    本就支持这五类，缺的只是编排侧入口。

    白名单限制在本类三类：PLAN / EXECUTE 仍走各自专用入口（它们的幂等键与
    历史命令序列已固化，不得经通用入口改变既有历史）。
    """
    if kind not in ("AUDIT", "INTEGRATE", "FINALIZE"):
        raise PermissionError(f"本入口仅准入 AUDIT/INTEGRATE/FINALIZE，拒绝 kind={kind}")
    return _admit_kind_action(activity_id, idempotency_key, expected_kind=kind)


def _admit_kind_action(
    activity_id: str,
    idempotency_key: str,
    *,
    expected_kind: str,
) -> dict[str, str | None]:
    from datetime import timedelta

    engine = _resolve_engine()
    subject = _worker_subject()
    aid = UUID(activity_id)
    with engine.connect() as db:
        kind = db.execute(
            text("SELECT kind FROM activities WHERE id=:id"),
            {"id": aid},
        ).scalar_one_or_none()
    if kind is None:
        raise LookupError(f"活动不存在: activity_id={activity_id}")
    if kind != expected_kind:
        raise PermissionError(f"本入口仅准入 {expected_kind}，拒绝 kind={kind}")
    lease = admit_runtime_attempt(
        engine,
        subject,
        idempotency_key,
        aid,
        lease_ttl=timedelta(seconds=1200),
    )
    if lease.lease is None:
        raise RuntimeError(f"admit_{expected_kind.lower()}_action 未返回租约")
    return {
        "activity_id": str(lease.lease.activity_id),
        "attempt_id": str(lease.lease.attempt_id),
        "fencing_epoch": str(lease.lease.fencing_epoch),
    }


# Activity 终态（观察用）；≠ 验收 PASS / Goal DONE
_ACTIVITY_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def read_activity_status(engine: Engine, activity_id: str) -> dict[str, str]:
    """读 activities.status/kind；供 Activity 与集成测共用。

    观察结果只反映库内状态行，不等于验收或 Goal DONE。
    """
    aid = UUID(activity_id)
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT status, kind FROM activities WHERE id=:id"),
                {"id": aid},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise LookupError(f"活动不存在: activity_id={activity_id}")
    return {
        "activity_id": str(aid),
        "status": str(row["status"]),
        "kind": str(row["kind"]),
    }


def is_activity_terminal_status(status: str) -> bool:
    """SUCCEEDED/FAILED/CANCELLED 视为观察终态。"""
    return status in _ACTIVITY_TERMINAL_STATUSES


@activity.defn(name="observe_activity_status")
def observe_activity_status(activity_id: str) -> dict[str, str]:
    """Kernel 观察 Activity 状态；不写库、不标 Goal DONE。"""
    return read_activity_status(_resolve_engine(), activity_id)


@activity.defn(name="observe_carried_plan_activity_status")
def observe_carried_plan_activity_status(
    goal_id: str,
    owner_epoch: str,
    activity_id: str,
) -> dict[str, str]:
    """校验 CAN 水位归属后观察 PLAN；拒绝跨 Goal、旧 owner 与非 PLAN。"""
    return _observe_carried_activity_status(
        goal_id,
        owner_epoch,
        activity_id,
        allowed_kinds=("PLAN",),
        mismatch_message="CAN 在途活动与当前 Goal/owner/PLAN 绑定不匹配",
    )


@activity.defn(name="observe_carried_activation_activity_status")
def observe_carried_activation_activity_status(
    goal_id: str,
    owner_epoch: str,
    activity_id: str,
) -> dict[str, str]:
    """校验 CAN 水位归属后观察 PLAN/EXECUTE（M3）；拒绝其它 kind 与跨绑定。"""
    return _observe_carried_activity_status(
        goal_id,
        owner_epoch,
        activity_id,
        allowed_kinds=("PLAN", "EXECUTE"),
        mismatch_message="CAN 在途活动与当前 Goal/owner/activation 绑定不匹配",
    )


def _observe_carried_activity_status(
    goal_id: str,
    owner_epoch: str,
    activity_id: str,
    *,
    allowed_kinds: tuple[str, ...],
    mismatch_message: str,
) -> dict[str, str]:
    gid = UUID(goal_id)
    aid = UUID(activity_id)
    with _resolve_engine().connect() as db:
        row = (
            db.execute(
                text(
                    """SELECT a.status, a.kind, a.goal_id,
                              g.owner_epoch AS goal_owner_epoch,
                              g.orchestration_backend,
                              b.owner_epoch AS binding_owner_epoch,
                              b.backend AS binding_backend
                    FROM activities a
                    JOIN goals g ON g.id=a.goal_id
                    JOIN orchestration_bindings b ON b.goal_id=g.id
                    WHERE a.id=:activity"""
                ),
                {"activity": aid},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise LookupError(f"活动不存在: activity_id={activity_id}")
    if (
        row["goal_id"] != gid
        or row["kind"] not in allowed_kinds
        or row["orchestration_backend"] != "TEMPORAL"
        or row["binding_backend"] != "TEMPORAL"
        or str(row["goal_owner_epoch"]) != str(owner_epoch)
        or str(row["binding_owner_epoch"]) != str(owner_epoch)
    ):
        raise PermissionError(mismatch_message)
    return {
        "activity_id": str(aid),
        "status": str(row["status"]),
        "kind": str(row["kind"]),
    }


@activity.defn(name="record_recovery_abandonment")
def record_recovery_abandonment(
    goal_id: str, reason: str, generation: int, prior_run_id: str | None
) -> dict[str, Any]:
    """把 Workflow 的放弃裁决**持久化为业务事实**（Issue #24）。

    为什么必须有这一步：Workflow 直接 `return` 只结束工作流，**不在 PG 留任何痕迹**；
    而 `workflow_id_for_goal` 是「每 Goal 一个 workflow ID」→ 重复驱动会被
    `WorkflowAlreadyStartedError` 去重挡回，**不会重启**。于是 Goal 静默永久卡住，
    且与「健康但缓慢」的 Goal 无法区分。

    权限与幂等由 Kernel 侧 `record_orchestration_abandonment` 保证：
    - 提交 `subject` 须为已登记 ACTIVE worker（绑定身份，非自报）；
    - 幂等键 `(goal_id, generation)`；同键原因不一致则 `PlanRejected` 冲突；
    - Goal 已终态则拒绝写入；**绝不**写 DONE；非终态 → `BLOCKED`（供人工介入）。

    `project_ids=[]`：编排 Activity 不持 JWT；Kernel 侧对该情形回退为
    「已登记 ACTIVE worker」校验（见其 docstring）。
    """
    engine = _resolve_engine()
    subject = _worker_subject()
    record = record_orchestration_abandonment(
        engine,
        UUID(goal_id),
        subject=subject,
        project_ids=[],
        reason=reason,
        generation=int(generation),
        prior_run_id=prior_run_id,
    )
    return {
        "abandonment_id": str(record["id"]),
        "goal_id": str(record["goal_id"]),
        "generation": int(record["generation"]),
        "reason": str(record["reason"]),
        "prior_run_id": record["prior_run_id"],
        "marks_goal_done": False,
    }


@activity.defn(name="request_goal_review")
def request_goal_review(
    goal_id: str, trigger_key: str, review_seq: int
) -> dict[str, Any]:
    """请求 Kernel 创建 GOAL_REVIEW 审查活动（幂等；**绝不**写 DONE）。

    为什么由编排侧发起：doc/v0.6/01 §7 规定「运行中 GoalReview 由 Workflow 的
    持久计时器/进度事件触发，**Kernel 用触发键去重**创建 AUDIT(target=GOAL_REVIEW)」。
    即：**计时归工作流**（可重放），**去重与快照钉扎归 Kernel**（业务事实）。
    本活动只是二者之间的接线，自身不含任何判断逻辑，也不改任何状态机。

    幂等由 Kernel 侧 `ensure_goal_review_activity` 保证：同 `trigger_key` 只生成一份
    （去重范围含在途与已成功）⇒ 工作流重试 / Continue-As-New 重入**不会**造出重复审查。
    返回 `created=False` 表示命中既有审查，属**正常**结果而非失败。

    失败关闭：Goal 已终态时 Kernel 抛 PlanRejected（不制造终态后的审查）。
    """
    engine = _resolve_engine()
    try:
        # **契约**：Kernel 返回 7 元组 (activity_id, digest, created, reviews_remaining,
        # max_reviews, min_interval_seconds, stagnation_seconds)；`review_seq` 必须 ≥1。
        # 逐项解包（而非 *rest）是为了让 Kernel 契约变化**立刻炸响**，不静默错位。
        (
            activity_id,
            digest,
            created,
            reviews_remaining,
            max_reviews,
            min_interval,
            stagnation,
        ) = ensure_goal_review_activity(
            engine,
            goal_id=UUID(goal_id),
            trigger_key=trigger_key,
            review_seq=int(review_seq),
        )
    except PlanRejected as exc:
        # 「不满足复盘条件」**不是**接线故障，而是设计内的失败关闭：工程仍在推进（未停滞）、
        # 间隔过短、或复盘预算耗尽时，Kernel 按不变量拒绝创建审查。
        # 故返回**结构化拒绝**（含原因码），让工作流区分二者：
        #   · 接线故障（解包/连接错）→ 抛错暴露，必须修；
        #   · 设计内拒绝 → 正常运行的一部分，记原因、不写 DONE、不当作失败计数。
        return {
            "activity_id": "",
            "review_snapshot_digest": "",
            "created": False,
            "deduped": False,
            "rejected": True,
            "reason_code": str(getattr(exc, "code", "") or ""),
            "reason": str(exc),
            "trigger_key": trigger_key,
            "review_seq": int(review_seq),
            "marks_goal_done": False,
        }
    return {
        "activity_id": str(activity_id),
        "review_snapshot_digest": str(digest),
        "created": bool(created),
        # created=False 表示命中既有审查（幂等去重），属正常结果
        "deduped": not bool(created),
        "rejected": False,
        "reviews_remaining": int(reviews_remaining),
        "max_reviews": int(max_reviews),
        "min_interval_seconds": int(min_interval),
        "stagnation_seconds": int(stagnation),
        "trigger_key": trigger_key,
        "review_seq": int(review_seq),
        "marks_goal_done": False,
    }


# 保留 M0 探测；GoalWorkflow 已改走控制 Activities
@activity.defn(name="ping_kernel")
def ping_kernel() -> dict[str, bool]:
    """控制面存活探测 stub；禁止在此写入 Goal DONE。"""
    return {"ok": True}
