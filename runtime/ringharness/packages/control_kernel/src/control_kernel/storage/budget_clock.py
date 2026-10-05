"""Goal 墙钟预算：Kernel 权威推进 elapsed_wall_seconds / active_seconds（Issue #20）。

- 自 DRAFT→PLANNING 起计；DRAFT 停留不计入
- elapsed：总墙钟，PAUSED 仍累计、不重置
- active：活跃秒，PAUSED/PAUSING/CANCELLING 不累计
- 单调不减；非法 budget_usage → UNKNOWN（不得当 0 消耗）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..protocols.goals import BudgetUsage
from ..protocols.runtime import LeaseRejected, PlanRejected
from .policies import ScopeNotFound

# NonnegativeInt 上界；向前跳跃时钳制，防止溢出回绕
_MAX_SECONDS = 9007199254740991

# 暂停态：总墙钟继续，活跃秒停表
_PAUSE_ACTIVE_STATUSES = frozenset({"PAUSED", "PAUSING", "CANCELLING"})

# 尚未启动：不推进
_NOT_STARTED = frozenset({"DRAFT"})

# 终态：仍可推进读数（排空），但 admit 另有门禁
_TERMINAL = frozenset({"DONE", "FAILED", "CANCELLED"})


class BudgetUsageUnknown(PlanRejected):
    """budget_usage 缺失/非法：失败关闭，不得解释为 0 消耗。"""

    def __init__(self, message: str = "BUDGET_USAGE_UNKNOWN: 预算用量不可用"):
        super().__init__(message)


def _parse_usage(raw: Any) -> BudgetUsage:
    if raw is None:
        raise BudgetUsageUnknown()
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise BudgetUsageUnknown()
        return BudgetUsage.model_validate(raw)
    except BudgetUsageUnknown:
        raise
    except Exception as exc:
        raise BudgetUsageUnknown() from exc


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def init_budget_clock_on_start(db: Connection, goal_id: UUID, *, now: datetime | None = None) -> BudgetUsage:
    """DRAFT→PLANNING：重置计量锚点；DRAFT 停留时间不计入。"""
    now = _as_utc(now or datetime.now(UTC))
    row = (
        db.execute(
            text("SELECT budget_usage FROM goals WHERE id=:id FOR UPDATE"),
            {"id": goal_id},
        )
        .mappings()
        .one()
    )
    usage = _parse_usage(row["budget_usage"])
    started = usage.model_copy(
        update={
            "elapsed_wall_seconds": 0,
            "active_seconds": 0,
            "observed_at": now,
        }
    )
    db.execute(
        text(
            """UPDATE goals SET budget_usage=CAST(:usage AS jsonb),
                updated_at=clock_timestamp()
            WHERE id=:id"""
        ),
        {"id": goal_id, "usage": started.model_dump_json()},
    )
    return started


def advance_goal_budget_clock(
    db: Connection,
    goal_id: UUID,
    *,
    now: datetime | None = None,
    goal_status: str | None = None,
) -> BudgetUsage:
    """在已持有 Goal 行锁的事务内推进墙钟；单调不减。

    调用方须已 `SELECT … FROM goals … FOR UPDATE`（或本函数自行加锁）。
    """
    now = _as_utc(now or datetime.now(UTC))
    row = (
        db.execute(
            text("SELECT status, budget_usage FROM goals WHERE id=:id FOR UPDATE"),
            {"id": goal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PlanRejected("Goal 不存在")
    status = goal_status if goal_status is not None else row["status"]
    usage = _parse_usage(row["budget_usage"])

    if status in _NOT_STARTED:
        # DRAFT：不推进；保持合法 0
        return usage

    last = _as_utc(usage.observed_at)
    if now < last:
        # 时钟回拨：不回退已记账秒数，也不回拨观察锚点
        return usage

    delta = int((now - last).total_seconds())
    delta = min(max(delta, 0), _MAX_SECONDS)

    new_elapsed = min(_MAX_SECONDS, int(usage.elapsed_wall_seconds) + delta)
    # 单调：即使解析异常也不会减小（再取 max）
    new_elapsed = max(int(usage.elapsed_wall_seconds), new_elapsed)

    if status in _PAUSE_ACTIVE_STATUSES:
        new_active = int(usage.active_seconds)
    else:
        new_active = min(_MAX_SECONDS, int(usage.active_seconds) + delta)
        new_active = max(int(usage.active_seconds), new_active)

    updated = usage.model_copy(
        update={
            "elapsed_wall_seconds": new_elapsed,
            "active_seconds": new_active,
            "observed_at": now,
        }
    )
    db.execute(
        text(
            """UPDATE goals SET budget_usage=CAST(:usage AS jsonb),
                updated_at=clock_timestamp()
            WHERE id=:id"""
        ),
        {"id": goal_id, "usage": updated.model_dump_json()},
    )
    return updated


def _contract_budget(contract: Any) -> Any:
    if isinstance(contract, str):
        contract = json.loads(contract)
    if isinstance(contract, dict):
        return contract.get("budget")
    return contract.budget


def wall_budget_limit_seconds(contract: Any) -> int:
    """从 Goal 合同读取 wall_clock_seconds；缺失则失败关闭。"""
    try:
        budget = _contract_budget(contract)
        if isinstance(budget, dict):
            limit = budget.get("wall_clock_seconds")
        else:
            limit = budget.wall_clock_seconds
        limit_i = int(limit)
        if limit_i < 1:
            raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: wall_clock_seconds 非法")
        return limit_i
    except BudgetUsageUnknown:
        raise
    except Exception as exc:
        raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: 合同预算不可读") from exc


def meter_budget_limits(contract: Any) -> tuple[int, int, int, int]:
    """读取 max_tokens / max_tool_calls / max_network_calls / max_disk_bytes。"""
    try:
        budget = _contract_budget(contract)
        if isinstance(budget, dict):
            tokens = budget.get("max_tokens")
            tools = budget.get("max_tool_calls")
            network = budget.get("max_network_calls")
            disk = budget.get("max_disk_bytes")
        else:
            tokens = budget.max_tokens
            tools = budget.max_tool_calls
            network = budget.max_network_calls
            disk = budget.max_disk_bytes
        tokens_i = int(tokens)
        tools_i = int(tools)
        network_i = int(network)
        disk_i = int(disk)
        if min(tokens_i, tools_i, network_i, disk_i) < 0:
            raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: meter 上限非法")
        return tokens_i, tools_i, network_i, disk_i
    except BudgetUsageUnknown:
        raise
    except Exception as exc:
        raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: 合同 meter 预算不可读") from exc


def cost_budget_limit_usd(contract: Any) -> Decimal:
    """读取 max_cost_usd；缺失/非法则失败关闭。"""
    try:
        budget = _contract_budget(contract)
        if isinstance(budget, dict):
            raw = budget.get("max_cost_usd")
        else:
            raw = budget.max_cost_usd
        limit = Decimal(str(raw))
        if limit < 0:
            raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: max_cost_usd 非法")
        return limit
    except BudgetUsageUnknown:
        raise
    except Exception as exc:
        raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: 合同 cost 预算不可读") from exc


def gpu_budget_limit_seconds(contract: Any) -> Decimal | None:
    """读取 max_gpu_seconds；null=无硬预算（仍可记录用量）；缺失键/非法 → UNKNOWN。"""
    try:
        budget = _contract_budget(contract)
        if isinstance(budget, dict):
            raw = budget.get("max_gpu_seconds")
        else:
            raw = budget.max_gpu_seconds
        if raw is None:
            return None
        limit = Decimal(str(raw))
        if limit < 0:
            raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: max_gpu_seconds 非法")
        return limit
    except BudgetUsageUnknown:
        raise
    except Exception as exc:
        raise BudgetUsageUnknown("BUDGET_USAGE_UNKNOWN: 合同 gpu 预算不可读") from exc


def _as_nonneg_decimal(value: str | Decimal, *, label: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PlanRejected(f"BUDGET_COST_INVALID: {label} 不是合法十进制") from exc
    if d < 0:
        raise PlanRejected(f"BUDGET_COST_NEGATIVE: {label} 不得为负")
    return d


def _to_decimal_string(value: Decimal) -> str:
    """格式化为 DecimalString；去掉尾随 0，避免违反契约正则。"""
    if value == 0:
        return "0"
    text_v = format(value, "f")
    if "." in text_v:
        text_v = text_v.rstrip("0").rstrip(".")
    return text_v or "0"


def _cost_exhausted(consumed: Decimal, limit: Decimal) -> bool:
    """max_cost_usd=0 表示禁止正花费（consumed=0 仍准入）；>0 时含边界耗尽。"""
    if limit == 0:
        return consumed > 0
    return consumed >= limit


def _gpu_exhausted(consumed: Decimal, limit: Decimal) -> bool:
    """与 cost 同形：max_gpu_seconds=0 禁正用量；>0 含边界。"""
    return _cost_exhausted(consumed, limit)


def _usage_gpu_seconds(usage: BudgetUsage) -> Decimal:
    """BudgetUsage.gpu_seconds 为 null 时按 0 累计（尚无可得用量）。"""
    if usage.gpu_seconds is None:
        return Decimal(0)
    return _as_nonneg_decimal(usage.gpu_seconds, label="gpu_seconds")


def goal_wall_budget_snapshot(
    db: Connection, goal_id: UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    """推进后快照，供编排层只读消费（剩余墙钟 / 是否耗尽）。"""
    row = (
        db.execute(
            text("SELECT status, contract, budget_usage FROM goals WHERE id=:id FOR UPDATE"),
            {"id": goal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PlanRejected("Goal 不存在")
    try:
        usage = advance_goal_budget_clock(db, goal_id, now=now, goal_status=row["status"])
        limit = wall_budget_limit_seconds(row["contract"])
        remaining = max(0, limit - int(usage.elapsed_wall_seconds))
        return {
            "budget_usage_unknown": False,
            "elapsed_wall_seconds": int(usage.elapsed_wall_seconds),
            "active_seconds": int(usage.active_seconds),
            "budget_remaining_wall_seconds": remaining,
            "budget_exhausted": int(usage.elapsed_wall_seconds) >= limit,
            "wall_clock_limit_seconds": limit,
        }
    except BudgetUsageUnknown:
        return {
            "budget_usage_unknown": True,
            "elapsed_wall_seconds": None,
            "active_seconds": None,
            "budget_remaining_wall_seconds": None,
            "budget_exhausted": True,
            "wall_clock_limit_seconds": None,
        }


def fail_goal_for_budget_exhausted(
    db: Connection,
    goal_id: UUID,
    *,
    reason: str | None = None,
    elapsed: int | None = None,
    limit: int | None = None,
) -> None:
    """预算耗尽：非终态 Goal → FAILED（doc/01:112）；保留未决 effect。

    取消 READY 工程活动；不杀 RUNNING（由失租/Stop 排空）；不写 DONE。
    可传完整 reason，或墙钟专用 elapsed/limit（兼容旧调用）。
    """
    row = (
        db.execute(
            text("SELECT status FROM goals WHERE id=:id FOR UPDATE"),
            {"id": goal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return
    if row["status"] in _TERMINAL:
        return
    if reason is None:
        if elapsed is None or limit is None:
            raise PlanRejected("BUDGET_EXHAUSTED_REASON_MISSING: 须提供 reason 或 elapsed/limit")
        reason = (
            f"BUDGET_EXHAUSTED: elapsed_wall_seconds={elapsed} >= wall_clock_seconds={limit}"
        )
    updated = (
        db.execute(
            text(
                """UPDATE goals SET status='FAILED', previous_status=:prev,
                    block_reason=:reason,
                    state_revision=state_revision+1, updated_at=clock_timestamp()
                WHERE id=:id AND status NOT IN ('DONE','FAILED','CANCELLED')
                RETURNING project_id, state_revision"""
            ),
            {"id": goal_id, "prev": row["status"], "reason": reason},
        )
        .mappings()
        .first()
    )
    if updated is None:
        return
    # 关闭新业务准入：取消尚未领取的 ENGINEERING
    db.execute(
        text(
            """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
            WHERE goal_id=:goal AND status='READY'
              AND kind = ANY(:kinds)"""
        ),
        {
            "goal": goal_id,
            "kinds": ["PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE"],
        },
    )
    from .events import append_goal_event

    append_goal_event(
        db,
        project_id=updated["project_id"],
        goal_id=goal_id,
        event_type="GOAL_STATE_CHANGED",
        entity_id=goal_id,
        entity_state_revision=updated["state_revision"],
        resource_type="GOAL",
    )


def assert_goal_wall_budget_allows_engineering(
    db: Connection, goal_id: UUID, *, now: datetime | None = None
) -> BudgetUsage:
    """危险动作（admit/claim ENGINEERING）前：推进并在耗尽/UNKNOWN 时失败关闭。"""
    row = (
        db.execute(
            text("SELECT status, contract FROM goals WHERE id=:id FOR UPDATE"),
            {"id": goal_id},
        )
        .mappings()
        .one()
    )
    try:
        usage = advance_goal_budget_clock(db, goal_id, now=now, goal_status=row["status"])
        limit = wall_budget_limit_seconds(row["contract"])
        max_tokens, max_tool_calls, max_network, max_disk = meter_budget_limits(
            row["contract"]
        )
        max_cost = cost_budget_limit_usd(row["contract"])
        max_gpu = gpu_budget_limit_seconds(row["contract"])
    except BudgetUsageUnknown as exc:
        raise LeaseRejected("BUDGET_USAGE_UNKNOWN", exc.message) from exc
    if int(usage.elapsed_wall_seconds) >= limit:
        fail_goal_for_budget_exhausted(
            db,
            goal_id,
            elapsed=int(usage.elapsed_wall_seconds),
            limit=limit,
        )
        raise LeaseRejected(
            "BUDGET_EXHAUSTED",
            f"墙钟预算耗尽：elapsed={usage.elapsed_wall_seconds} >= limit={limit}",
        )
    if int(usage.consumed_tokens) + int(usage.reserved_tokens) >= max_tokens:
        reason = (
            f"BUDGET_EXHAUSTED: consumed_tokens+reserved_tokens="
            f"{usage.consumed_tokens}+{usage.reserved_tokens} >= max_tokens={max_tokens}"
        )
        fail_goal_for_budget_exhausted(db, goal_id, reason=reason)
        raise LeaseRejected("BUDGET_EXHAUSTED", reason)
    if int(usage.tool_calls) >= max_tool_calls:
        reason = (
            f"BUDGET_EXHAUSTED: tool_calls={usage.tool_calls} >= max_tool_calls={max_tool_calls}"
        )
        fail_goal_for_budget_exhausted(db, goal_id, reason=reason)
        raise LeaseRejected("BUDGET_EXHAUSTED", reason)
    if int(usage.network_calls) >= max_network:
        reason = (
            f"BUDGET_EXHAUSTED: network_calls={usage.network_calls}"
            f" >= max_network_calls={max_network}"
        )
        fail_goal_for_budget_exhausted(db, goal_id, reason=reason)
        raise LeaseRejected("BUDGET_EXHAUSTED", reason)
    if int(usage.disk_bytes) >= max_disk:
        reason = (
            f"BUDGET_EXHAUSTED: disk_bytes={usage.disk_bytes} >= max_disk_bytes={max_disk}"
        )
        fail_goal_for_budget_exhausted(db, goal_id, reason=reason)
        raise LeaseRejected("BUDGET_EXHAUSTED", reason)
    committed_cost = _as_nonneg_decimal(
        usage.consumed_cost_usd, label="consumed_cost_usd"
    ) + _as_nonneg_decimal(usage.reserved_cost_usd, label="reserved_cost_usd")
    if _cost_exhausted(committed_cost, max_cost):
        reason = (
            f"BUDGET_EXHAUSTED: consumed_cost_usd+reserved_cost_usd="
            f"{usage.consumed_cost_usd}+{usage.reserved_cost_usd}"
            f" vs max_cost_usd={_to_decimal_string(max_cost)}"
        )
        fail_goal_for_budget_exhausted(db, goal_id, reason=reason)
        raise LeaseRejected("BUDGET_EXHAUSTED", reason)
    if max_gpu is not None and _gpu_exhausted(_usage_gpu_seconds(usage), max_gpu):
        reason = (
            f"BUDGET_EXHAUSTED: gpu_seconds={usage.gpu_seconds}"
            f" vs max_gpu_seconds={_to_decimal_string(max_gpu)}"
        )
        fail_goal_for_budget_exhausted(db, goal_id, reason=reason)
        raise LeaseRejected("BUDGET_EXHAUSTED", reason)
    return usage


def tick_goal_budget_after_heartbeat(
    engine: Engine,
    goal_id: UUID,
    *,
    now: datetime | None = None,
) -> None:
    """heartbeat 成功后续约后推进墙钟（独立事务）。

    与 renew_lease 分事务：避免 attempt FOR UPDATE → goal FOR UPDATE 与
    claim 的 admission→attempt 锁序相反导致死锁。
    耗尽时将 Goal 置 FAILED，不抛错打断已成功的续约（Runner 可收 Stop/排空）。
    非法 budget_usage：本拍跳过推进（下一次 claim/snapshot 仍失败关闭）。
    """
    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT status, contract FROM goals WHERE id=:id FOR UPDATE"),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return
        try:
            usage = advance_goal_budget_clock(
                db, goal_id, now=now, goal_status=row["status"]
            )
            limit = wall_budget_limit_seconds(row["contract"])
        except BudgetUsageUnknown:
            return
        if int(usage.elapsed_wall_seconds) >= limit:
            fail_goal_for_budget_exhausted(
                db,
                goal_id,
                elapsed=int(usage.elapsed_wall_seconds),
                limit=limit,
            )


def _maybe_fail_goal_for_meter_exhaustion(
    db: Connection,
    goal_id: UUID,
    usage: BudgetUsage,
    contract: Any,
) -> None:
    """meter/cost/gpu 达上限则 FAILED；不抛错（回执路径须保留 APPLIED）。"""
    try:
        max_tokens, max_tool_calls, max_network, max_disk = meter_budget_limits(contract)
        max_cost = cost_budget_limit_usd(contract)
        max_gpu = gpu_budget_limit_seconds(contract)
    except BudgetUsageUnknown:
        return
    if int(usage.consumed_tokens) + int(usage.reserved_tokens) >= max_tokens:
        fail_goal_for_budget_exhausted(
            db,
            goal_id,
            reason=(
                f"BUDGET_EXHAUSTED: consumed_tokens+reserved_tokens="
                f"{usage.consumed_tokens}+{usage.reserved_tokens}"
                f" >= max_tokens={max_tokens}"
            ),
        )
        return
    if int(usage.tool_calls) >= max_tool_calls:
        fail_goal_for_budget_exhausted(
            db,
            goal_id,
            reason=(
                f"BUDGET_EXHAUSTED: tool_calls={usage.tool_calls}"
                f" >= max_tool_calls={max_tool_calls}"
            ),
        )
        return
    if int(usage.network_calls) >= max_network:
        fail_goal_for_budget_exhausted(
            db,
            goal_id,
            reason=(
                f"BUDGET_EXHAUSTED: network_calls={usage.network_calls}"
                f" >= max_network_calls={max_network}"
            ),
        )
        return
    if int(usage.disk_bytes) >= max_disk:
        fail_goal_for_budget_exhausted(
            db,
            goal_id,
            reason=(
                f"BUDGET_EXHAUSTED: disk_bytes={usage.disk_bytes}"
                f" >= max_disk_bytes={max_disk}"
            ),
        )
        return
    committed_cost = _as_nonneg_decimal(
        usage.consumed_cost_usd, label="consumed_cost_usd"
    ) + _as_nonneg_decimal(usage.reserved_cost_usd, label="reserved_cost_usd")
    if _cost_exhausted(committed_cost, max_cost):
        fail_goal_for_budget_exhausted(
            db,
            goal_id,
            reason=(
                f"BUDGET_EXHAUSTED: consumed_cost_usd+reserved_cost_usd="
                f"{usage.consumed_cost_usd}+{usage.reserved_cost_usd}"
                f" vs max_cost_usd={_to_decimal_string(max_cost)}"
            ),
        )
        return
    if max_gpu is not None and _gpu_exhausted(_usage_gpu_seconds(usage), max_gpu):
        fail_goal_for_budget_exhausted(
            db,
            goal_id,
            reason=(
                f"BUDGET_EXHAUSTED: gpu_seconds={usage.gpu_seconds}"
                f" vs max_gpu_seconds={_to_decimal_string(max_gpu)}"
            ),
        )


def adjust_goal_budget_reservations(
    db: Connection,
    goal_id: UUID,
    *,
    reserved_tokens_delta: int = 0,
    reserved_cost_usd_delta: str | Decimal | None = None,
    now: datetime | None = None,
) -> BudgetUsage:
    """调整预留 token/cost（ModelInvocation 登记+/终态回执-）。

    - 预留可增可减；减至 0 钳制，不得记为负
    - UNKNOWN 回执不释放（由调用方决定不调减）
    - 达上限 → Goal FAILED（≠ DONE）；登记超限由调用方 raise 回滚
    """
    if reserved_cost_usd_delta is None:
        cost_delta = Decimal(0)
    else:
        raw = Decimal(str(reserved_cost_usd_delta))
        if raw >= 0:
            cost_delta = _as_nonneg_decimal(raw, label="reserved_cost_usd_delta")
        else:
            cost_delta = -_as_nonneg_decimal(-raw, label="reserved_cost_usd_delta")

    if int(reserved_tokens_delta) == 0 and cost_delta == 0:
        row = (
            db.execute(
                text("SELECT budget_usage FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        return _parse_usage(row["budget_usage"])

    now = _as_utc(now or datetime.now(UTC))
    row = (
        db.execute(
            text(
                "SELECT budget_usage, contract FROM goals WHERE id=:id FOR UPDATE"
            ),
            {"id": goal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ScopeNotFound()
    usage = _parse_usage(row["budget_usage"])
    new_tokens = max(int(usage.reserved_tokens) + int(reserved_tokens_delta), 0)
    new_tokens = min(_MAX_SECONDS, new_tokens)
    new_cost = _as_nonneg_decimal(
        usage.reserved_cost_usd, label="reserved_cost_usd"
    ) + cost_delta
    new_cost = max(new_cost, Decimal(0))
    updated = usage.model_copy(
        update={
            "reserved_tokens": new_tokens,
            "reserved_cost_usd": _to_decimal_string(new_cost),
            "observed_at": now,
        }
    )
    db.execute(
        text(
            """UPDATE goals SET budget_usage=CAST(:usage AS jsonb),
            updated_at=clock_timestamp() WHERE id=:id"""
        ),
        {"id": goal_id, "usage": updated.model_dump_json()},
    )
    _maybe_fail_goal_for_meter_exhaustion(db, goal_id, updated, row["contract"])
    return updated


def record_goal_budget_meters(
    db: Connection,
    goal_id: UUID,
    *,
    consumed_tokens_delta: int = 0,
    tool_calls_delta: int = 0,
    network_calls_delta: int = 0,
    disk_bytes_delta: int = 0,
    consumed_cost_usd_delta: str | Decimal | None = None,
    gpu_seconds_delta: str | Decimal | None = None,
    now: datetime | None = None,
) -> BudgetUsage:
    """单调累加 token / tool / network / disk / cost / gpu（回执咽喉）。

    - delta < 0 → 拒绝（不得回拨）
    - 非法 budget_usage → BudgetUsageUnknown
    - 达合同上限 → Goal FAILED（≠ DONE）；不抛错
    - max_gpu_seconds=null 仍记录 gpu_seconds，但不因 GPU 硬预算 FAILED
    """
    cost_delta = (
        Decimal(0)
        if consumed_cost_usd_delta is None
        else _as_nonneg_decimal(consumed_cost_usd_delta, label="consumed_cost_usd_delta")
    )
    gpu_delta = (
        Decimal(0)
        if gpu_seconds_delta is None
        else _as_nonneg_decimal(gpu_seconds_delta, label="gpu_seconds_delta")
    )
    if (
        int(consumed_tokens_delta) < 0
        or int(tool_calls_delta) < 0
        or int(network_calls_delta) < 0
        or int(disk_bytes_delta) < 0
    ):
        raise PlanRejected("BUDGET_METER_DELTA_NEGATIVE: 计量增量不得为负")
    if (
        int(consumed_tokens_delta) == 0
        and int(tool_calls_delta) == 0
        and int(network_calls_delta) == 0
        and int(disk_bytes_delta) == 0
        and cost_delta == 0
        and gpu_delta == 0
    ):
        row = (
            db.execute(
                text("SELECT budget_usage FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        return _parse_usage(row["budget_usage"])

    now = _as_utc(now or datetime.now(UTC))
    row = (
        db.execute(
            text(
                "SELECT budget_usage, contract FROM goals WHERE id=:id FOR UPDATE"
            ),
            {"id": goal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ScopeNotFound()
    usage = _parse_usage(row["budget_usage"])
    tokens = min(
        _MAX_SECONDS,
        int(usage.consumed_tokens) + int(consumed_tokens_delta),
    )
    tools = min(_MAX_SECONDS, int(usage.tool_calls) + int(tool_calls_delta))
    network = min(_MAX_SECONDS, int(usage.network_calls) + int(network_calls_delta))
    disk = min(_MAX_SECONDS, int(usage.disk_bytes) + int(disk_bytes_delta))
    new_cost = _as_nonneg_decimal(usage.consumed_cost_usd, label="consumed_cost_usd") + cost_delta
    new_gpu = _usage_gpu_seconds(usage) + gpu_delta
    update_fields: dict[str, Any] = {
        "consumed_tokens": tokens,
        "tool_calls": tools,
        "network_calls": network,
        "disk_bytes": disk,
        "consumed_cost_usd": _to_decimal_string(new_cost),
        "cost_status": "CONFIRMED" if cost_delta > 0 else usage.cost_status,
        "observed_at": now,
    }
    if gpu_delta > 0 or usage.gpu_seconds is not None:
        update_fields["gpu_seconds"] = _to_decimal_string(new_gpu)
    updated = usage.model_copy(update=update_fields)
    db.execute(
        text(
            """UPDATE goals SET budget_usage=CAST(:usage AS jsonb),
            updated_at=clock_timestamp() WHERE id=:id"""
        ),
        {"id": goal_id, "usage": updated.model_dump_json()},
    )
    _maybe_fail_goal_for_meter_exhaustion(db, goal_id, updated, row["contract"])
    return updated
