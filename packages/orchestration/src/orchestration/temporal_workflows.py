"""Temporal replay-safe Workflow 定义（确定性；禁止 SQL/HTTP/文件 IO）。

IO 只能通过 Activity 名称调度；本模块不得 import control_kernel.storage / httpx / 文件 API。
GoalWorkflow：ensure → list → admit PLAN/EXECUTE → RunActivation(ring-runner)
→ observe_activity_status（activation 终态观察）；
仅返回 id / pending_harness / plan_terminal_statuses 摘要，**不得**将 Goal 标为 DONE。
观察终态 ≠ 验收 PASS；Goal 仍由 Kernel 持有。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception

# Workflow 沙箱内字面量；与 kernel_activities 终态集合对齐
_ACTIVATION_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
# 失租标记：租约过期时 Kernel 把活动置 RECOVERING（见 storage/claims.py expire_stale_leases）。
# 它是**非终态**，故不写这个常量的话，工作流无法把「租约已丢、工作已废」与「正常推进」区分开。
_ACTIVITY_LEASE_LOST_STATUS = "RECOVERING"
# 生产默认观察轮次；测试可通过 payload.observe_max_ticks 缩小
_DEFAULT_OBSERVE_MAX_TICKS = 30

# 观察轮询退避（Issue：健康的长任务被误判 OBSERVE_TIMEOUT）。
# 为什么需要：默认 30 轮 × 原固定 1s ≈ **仅 30 秒**观察窗口；而本仓 harness 实跑以分钟计
# （`RunActivation` start_to_close 360s、本机 Qwen 超时 180s）。窗口过短会让健康任务被判
# OBSERVE_TIMEOUT → CAN → 次数上限 → `RECOVERY_ABANDONED`，把正常长任务当作不可恢复放弃。
# 退避后 29 次等待合计约 24 分钟：快完成的任务首轮即命中（不白等），长任务不被误杀。
_OBSERVE_BACKOFF_BASE_SECONDS = 1
_OBSERVE_BACKOFF_CAP_SECONDS = 60
_OBSERVE_BACKOFF_MAX_EXPONENT = 6

# ---------------------------------------------------------------------------
# RunActivation 派发预算（start_to_close）
#
# 为什么需要可配：EXECUTE 阶段的官方 AgentLoop 要跑**多轮**模型往返
# （读→测→改→复测→封存），而 Runner 单侧单次模型调用的 idle 超时默认 180s。
# 钉扎的 360s 只够约两轮 —— 健康的自主多步循环会被 Activity 超时打断，
# 且打断后走的是「转入 Kernel 状态核对」而非明确报错，属静默降级。
#
# 约束：**只许放大，不许缩小**。地板 = 钉扎值 360s；显式值低于地板视为配置错误并抛错
# （失败关闭），避免误配把预算压到比单次模型调用还短。
# 预算进入 ScheduleActivityTask 命令属性 ⇒ 改变会影响重放 ⇒ 由 patch 门保护旧历史。
# ---------------------------------------------------------------------------
_DEFAULT_ACTIVATION_BUDGET_SECONDS = 360
_ACTIVATION_BUDGET_FLOOR_SECONDS = 360
_ACTIVATION_BUDGET_PATCH = "activation-budget-v1"


def _resolve_activation_budget_seconds(raw: object) -> int:
    """RunActivation start_to_close 预算（秒）—— 纯函数，便于单测；不做 IO。

    缺省/空 → 钉扎值 360（与 AGENTS §9 一致）。
    显式 → 须为整数且 >= 地板；否则抛 ValueError（**失败关闭，不静默取默认**）。

    ⚠️ **不要在工作流代码里直接调用本函数**（会抛）。工作流内请用
    `_effective_activation_budget_seconds`：工作流抛出异常会让 Workflow Task
    反复失败重试，表现为「工作流永久挂住」而非明确报错（本批实测，见该函数说明）。
    本函数的抛错语义适用于**启动/API 边界**，那里调用方能收到明确的 4xx。
    """
    if raw is None or raw == "":
        return _DEFAULT_ACTIVATION_BUDGET_SECONDS
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"activation_budget_seconds 须为整数，收到 {raw!r}") from exc
    if value < _ACTIVATION_BUDGET_FLOOR_SECONDS:
        raise ValueError(
            f"activation_budget_seconds={value} 低于地板 {_ACTIVATION_BUDGET_FLOOR_SECONDS}s；"
            "本预算须容纳 Runner 单次模型调用超时（默认 180s）与多轮工具循环，只许放大"
        )
    return value


def _effective_activation_budget_seconds(
    raw: object, *, observe_max_ticks: int
) -> tuple[int, str | None]:
    """工作流内使用的**不抛错**版本，返回 `(生效预算, 未采用显式值的原因或 None)`。

    为什么不抛错（本批实测结论）：在工作流代码里 `raise` 会让 Workflow Task
    持续失败并被 Temporal 反复重试，结果是**工作流永久挂住**、既不完成也不报错 ——
    比「用错预算」更糟。故此处以「安全退回钉扎值 + 回报原因」表达冲突：
    调用方可把原因写进工作流返回值，使误配**可见**而非静默。

    规则（安全优先）：
    - 显式值非法（非整数 / 低于地板）→ 退回钉扎值；
    - 显式值超过观察窗口 → 退回钉扎值（否则活动仍合法运行时工作流已判超时，
      等于制造新的静默误杀）；
    - 其余 → 采用显式值（只许放大已由地板保证）。
    """
    if raw is None or raw == "":
        return _DEFAULT_ACTIVATION_BUDGET_SECONDS, None
    try:
        requested = _resolve_activation_budget_seconds(raw)
    except ValueError as exc:
        return _DEFAULT_ACTIVATION_BUDGET_SECONDS, f"explicit_budget_rejected:{exc}"
    window = _observe_window_seconds(observe_max_ticks)
    if requested > window:
        return (
            _DEFAULT_ACTIVATION_BUDGET_SECONDS,
            f"budget_exceeds_observe_window:{requested}>{window}",
        )
    return requested, None


def _observe_window_seconds(max_ticks: int) -> int:
    """观察窗口总时长（秒）—— 纯函数：观察循环里**实际发生**的等待之和。

    与观察循环同口径：轮次为 `0 .. max_ticks-1`，且**末轮不再 sleep**
    （见循环内 `if tick + 1 < observe_max_ticks`），故等待次数为 `max_ticks-1`。
    刻意与循环写法逐字对应，避免「窗口算错一轮」这类只能靠事故发现的偏差。
    """
    ticks = max(1, int(max_ticks))
    return sum(_observe_poll_delay_seconds(t) for t in range(max(0, ticks - 1)))


# ---------------------------------------------------------------------------
# 有界重试（doc/v0.6 §5.5：「技术错误允许**有限**退避；业务失败进入 Kernel 决策」）
#
# 缺陷背景：多处 `execute_activity` 未传 `retry_policy`，而 Temporal 的**默认是无限重试**
# （`maximum_attempts=0`）。后果是静默挂死：持续失败的 Kernel IO 会一直重试，
# 工作流既不到 OBSERVE_TIMEOUT、也不推进 recovery_attempts、更不进入
# RECOVERY_ABANDONED —— 整个恢复设计被无限重试绕过。
# 佐证这是疏漏而非设计：同一观察动作的另一分支（carried）**显式**设了
# `maximum_attempts=1`，RunActivation 也显式设了；两处并列分支却不对称。
# ---------------------------------------------------------------------------
_KERNEL_IO_MAX_ATTEMPTS = 3
_OBSERVE_MAX_ATTEMPTS = 1
# 活动重试策略进入 ScheduleActivityTask 命令属性，改变它会影响重放 →
# 以 patch 门保护旧历史（无 marker 时逐字保留原行为：不传 retry_policy）。
_ACTIVITY_RETRY_BOUNDED_PATCH = "activity-retry-bounded-v1"


def _bounded_retry_policy(*, maximum_attempts: int) -> RetryPolicy:
    """构造**有界**重试策略 —— 纯函数，便于单测。

    `maximum_attempts=0` 在 Temporal 语义中是**无限重试**，正是本批要消除的行为，
    故此处直接拒绝该取值，避免调用方无意中把它写回来。
    """
    if maximum_attempts < 1:
        raise ValueError("maximum_attempts 必须 >= 1；0 表示无限重试（禁止）")
    return RetryPolicy(
        maximum_attempts=maximum_attempts,
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=10),
    )


def _observe_poll_delay_seconds(tick: int) -> int:
    """第 `tick` 轮之后应等待的秒数 —— 纯函数，便于单测与重放确定性。

    指数退避并封顶：`1, 2, 4, 8, 16, 32, 60, 60, …`
    """
    if tick < 0:
        return _OBSERVE_BACKOFF_BASE_SECONDS
    exponent = min(tick, _OBSERVE_BACKOFF_MAX_EXPONENT)
    return min(
        _OBSERVE_BACKOFF_BASE_SECONDS << exponent, _OBSERVE_BACKOFF_CAP_SECONDS
    )
# M3：旧历史 list 可能已含 EXECUTE 但曾被忽略；无本 patch 时不得新增 admit/RunActivation
_M3_EXECUTE_ORCHESTRATION_PATCH = "m3-execute-orchestration-v1"
# 门3 恢复安全：旧历史无本 patch 时不得插入 generation>0 提前放弃分支
_RECOVERY_SAFETY_GATE_PATCH = "recovery-safety-gate-v1"
# 门3 收口：独立恢复总开关；旧历史无本 patch 时不得新增 RECOVERY_DISABLED 分支
_RECOVERY_ENABLED_SWITCH_PATCH = "recovery-enabled-switch-v1"
# 观察轮询退避：等待时长也是**定时器命令**，改变它会改变命令序列，
# 故必须以 patch 门保护旧历史重放（无本 marker 时逐字沿用固定 1s）。
_OBSERVE_BACKOFF_PATCH = "observe-backoff-v1"
# 放弃裁决持久化（Issue #24）：新增 Activity 调用 = 新增命令，
# 无本 marker 的旧历史逐字沿用「只 return 不落库」的旧行为。
_ABANDON_PERSISTENCE_PATCH = "abandon-persistence-v1"

# CAN 续跑载荷透传激活预算：CAN 的 input 载荷属于**命令内容**，新增键会使旧历史
# 重放报非确定性失败（2026-09-25 实测：advance-on-ready CAN 漏带
# activation_budget_seconds，续跑 RunActivation 回退钉扎 360s，DeepSeek 多轮
# 工具循环再次被 StartToClose 打断）。无本 marker 的旧历史逐字沿用不带该键。
_CAN_BUDGET_CARRY_PATCH = "can-budget-carry-v1"

# 周期 GoalReview（doc/v0.6/01 §7）：**计时归 Workflow**（可重放），
# **去重与快照钉扎归 Kernel**（业务事实）；频率/上限必须配置且**不得无限复盘**。
_GOAL_REVIEW_PATCH = "goal-review-periodic-v1"
# 修复 review_seq 基数的补丁：旧历史记录的是 0 基 seq，改成 1 基会变更命令参数
# ⇒ 属重放危险，必须 gating（否则旧历史重放报非确定性失败）。
_GOAL_REVIEW_SEQ_BASE_PATCH = "goal-review-seq-base-v1"

# 跨活动推进（2026-09-15 实测缺口，见 doc/engineering/跨活动推进缺口-调研结论-2026-09-15.md）。
#
# 问题：本工作流是「一次运行 = 一批活动」——取一次 READY 动作、admit、派发、观察、**return**。
# 而下一条命令的新 run 只由 ENSURE_WORKFLOW 投递触发，该投递全仓**仅在 START 入队**
# （`enqueue_ensure_workflow` 唯一调用点）。于是 PLAN 成功后新 READY 的 EXECUTE
# 无人推进：实测 Goal 静默停在 `PLAN:SUCCEEDED + EXECUTE:READY` **二十余分钟无变化**，
# 手工补一次触发后 15s 内即 READY→RUNNING、effects 0→6。
#
# 修法：本轮有**实质进展**且 Kernel 仍报有 READY 工作 → Continue-As-New 续跑，
# 由同一条 GoalWorkflow 继续 drain 就绪工作。用 CAN 而非新 run：CAN 是同一条工作流的
# 续跑，不会与自身并发；新 run 会撞同 workflow_id 造成重叠。
# 旧历史无本 marker ⇒ 逐字沿用「直接 return」（可重放）。
_ADVANCE_ON_READY_PATCH = "advance-on-ready-v1"
# 续跑跳数上限：失败关闭的兜底，防意外自激（每跳都做真实工作，正常远达不到）
_DEFAULT_MAX_ADVANCE_HOPS = 200
# 载荷里保留的「已推进活动 id」条数上限。**必须有界**：无界累加会把单条 CAN 载荷
# 撑爆（实测 200 条 ⇒ 8457 字符），而 temporal-history-v1 要求单条 payload < 4096。
# 保留最近若干条已足够防「同一活动被反复推进」。
_ADVANCE_TRACKED_IDS_MAX = 64

# 全类活动准入（2026-09-15 实测缺口，与上一条同源）。
#
# 问题：编排工作流只准入 PLAN 与 EXECUTE；而 Kernel 在 EXECUTE 成功后**自动创建**
# AUDIT（验收）→ INTEGRATE / FINALIZE（收口）。这三类无人准入 ⇒ 目标永远到不了 DONE。
# 实测：`PLAN:SUCCEEDED → EXECUTE:SUCCEEDED → AUDIT:READY` 之后就静止不动。
# 仓库那条「能到 DONE」的 e2e 缝是**测试自己**逐个手推五类活动 —— 它站在编排层
# 缺位的地方代劳，故「一层的 live 绿」掩盖了本缺口。
# 旧历史无本 marker ⇒ 维持「仅 PLAN/EXECUTE」的既有命令序列（可重放）。
_ADMIT_ALL_KINDS_PATCH = "admit-all-kinds-v1"
# 三权 Runner 队列：旧 history 继续使用 ring-runner；新启动载荷显式开启后按 kind 分流。
_RUNNER_AUTHORITY_QUEUES_PATCH = "runner-authority-queues-v1"

_LEGACY_RUNNER_TASK_QUEUE = "ring-runner"
_MANAGER_RUNNER_TASK_QUEUE = "ring-runner-manager"
_EXECUTOR_RUNNER_TASK_QUEUE = "ring-runner-executor"
_AUDITOR_RUNNER_TASK_QUEUE = "ring-runner-auditor"


def _runner_task_queue_for_kind(kind: str) -> str:
    """确定性映射 Activity kind 到三权 Runner 队列。"""
    if kind == "PLAN":
        return _MANAGER_RUNNER_TASK_QUEUE
    if kind in ("EXECUTE", "INTEGRATE"):
        return _EXECUTOR_RUNNER_TASK_QUEUE
    if kind in ("AUDIT", "FINALIZE"):
        return _AUDITOR_RUNNER_TASK_QUEUE
    return _LEGACY_RUNNER_TASK_QUEUE
_DEFAULT_MAX_GOAL_REVIEWS = 3
_GOAL_REVIEW_MIN_INTERVAL_SECONDS = 30
# 门3 恢复安全：CAN payload schema；与 Activity Checkpoint.schema_version 分离
_RECOVERY_CHECKPOINT_SCHEMA_VERSION = 1
_DEFAULT_MAX_RECOVERY_ATTEMPTS = 3
# Hermes 裁定（研究附录采纳裁定 §门3）：窗口优先级 = 显式 payload 覆盖
#   > min(Goal 剩余 wall_clock, 上限) > 本过渡默认。
# 为何不照搬书中固定 4h：本仓是长时无人值守（Goal wall_clock 可达 5 天量级），
# 固定窗口会把正常的长 Goal 恢复误判为过期。
_DEFAULT_INTENT_TTL_SECONDS = 4 * 60 * 60
# 上限：单次恢复窗口不得超过此时长，避免一次恢复覆盖整个长 Goal 预算
_INTENT_TTL_CAP_SECONDS = 4 * 60 * 60


def _resolve_intent_ttl_seconds(
    *,
    explicit_ttl_seconds: int | None,
    budget_remaining_seconds: int | None,
) -> int:
    """恢复意图窗口（秒）—— 纯函数，便于单测；不读时钟、不做 IO。

    优先级（裁定 §门3，经 Codex 复核修正）：
    1. 有权威剩余预算 → `min(剩余, _INTENT_TTL_CAP_SECONDS)`；
       **显式 TTL 只能收紧、不能放大**权威剩余（Codex P1-3 第 2 点半）。
    2. 无权威读数（老历史 / 预算不可读）→ 显式 TTL，否则过渡默认。

    权威剩余量来自 Kernel 预算账 `goals.budget_usage.elapsed_wall_seconds`，
    **不**来自 `created_at`：Goal 可长期停留 DRAFT，用创建时刻会把刚启动的 Goal
    算成已过期（Codex P1-1）。耗尽（remaining<=0）由调用方先行失败关闭，
    故本函数不负责表达"不可恢复"，只保证返回值 >= 1。
    """
    if budget_remaining_seconds is not None:
        capped = min(budget_remaining_seconds, _INTENT_TTL_CAP_SECONDS)
        if explicit_ttl_seconds is not None:
            return max(1, min(explicit_ttl_seconds, capped))
        return max(1, capped)
    if explicit_ttl_seconds is not None:
        return explicit_ttl_seconds
    return _DEFAULT_INTENT_TTL_SECONDS


def _truthy_flag(raw: object) -> bool:
    """仅显式 true/1/yes 为开；缺省与假值一律关闭（失败关闭）。"""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes")


def _parse_intent_deadline(raw: object):
    """解析 intent_valid_until；非法或无时区则视为不兼容放弃。"""
    from datetime import UTC, datetime

    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("intent_valid_until 必须带时区")
    return parsed.astimezone(UTC)


def _workflow_now_utc():
    """workflow.now() 可能无 tzinfo；统一为 UTC aware 再与 deadline 比较。"""
    from datetime import UTC

    now = workflow.now()
    if getattr(now, "tzinfo", None) is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _recovery_abandon_result(
    *,
    command_id: str,
    goal_id: str,
    generation: int,
    prior_run_id: str | None,
    reason: str,
    delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """恢复放弃摘要；绝不 marks_goal_done。"""
    delivery = delivery or {}
    return {
        "ok": False,
        "command_id": str(delivery.get("command_id") or command_id),
        "goal_id": goal_id,
        "workflow_id": delivery.get("workflow_id"),
        "run_id": delivery.get("run_id"),
        "delivery_status": delivery.get("delivery_status"),
        "action_ids": [],
        "admitted_activity_ids": [],
        "admitted_attempt_ids": [],
        "run_activation_results": [],
        "pending_harness": False,
        "plan_terminal_statuses": [],
        "generation": generation,
        "prior_run_id": prior_run_id,
        "command_ids_consumed": [],
        "continued_as_new": False,
        "recovery_status": "RECOVERY_ABANDONED",
        "recovery_reason": reason,
        "marks_goal_done": False,
    }


@workflow.defn(name="GoalWorkflow")
class GoalWorkflow:
    """控制环：Kernel Activities + Runner RunActivation + PLAN/EXECUTE 观察；不写业务终态。

    生产 Continue-As-New 水位（observe-timeout 恢复路径）已部分落地：
    可选 payload 携带 command_ids_consumed / skip_admit_activity_ids / generation /
    prior_run_id；测试经 enable_continue_as_new 显式开启，生产可稍后经 env→payload 打开。
    不得清零预算或已消费 command_id；模式亦见 TM08 CommandWaterlineWorkflow。
    """

    @workflow.run
    async def run(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        command_id = str(data.get("command_id") or "")
        goal_id = str(data.get("goal_id") or "")
        owner_epoch = str(data.get("owner_epoch") or "1")
        if not command_id or not goal_id:
            raise ValueError("GoalWorkflow 需要 command_id 与 goal_id")

        # Continue-As-New 水位（可选；缺省不启用 CAN）
        command_ids_consumed = [str(x) for x in (data.get("command_ids_consumed") or [])]
        skip_admit_activity_ids = [
            str(x) for x in (data.get("skip_admit_activity_ids") or [])
        ]
        skip_admit_set = set(skip_admit_activity_ids)
        generation = int(data.get("generation") or 0)
        raw_prior = data.get("prior_run_id")
        prior_run_id = str(raw_prior) if raw_prior else None
        enable_continue_as_new = bool(data.get("enable_continue_as_new") or False)
        raw_can_timeout = data.get("continue_as_new_on_observe_timeout")
        if raw_can_timeout is None:
            # 开启 CAN 时默认在 OBSERVE_TIMEOUT 时续跑
            continue_as_new_on_observe_timeout = enable_continue_as_new
        else:
            continue_as_new_on_observe_timeout = bool(raw_can_timeout)
        carried_attempt_ids = [str(x) for x in (data.get("admitted_attempt_ids") or [])]

        # 周期 GoalReview 旋钮（缺省 0 = 关闭；两值都须 >0 才启用 —— 失败关闭）
        raw_review_interval = data.get("goal_review_interval_seconds")
        goal_review_interval_seconds = (
            max(0, int(raw_review_interval))
            if raw_review_interval not in (None, "")
            else 0
        )
        raw_max_reviews = data.get("max_goal_reviews")
        max_goal_reviews = (
            max(0, int(raw_max_reviews)) if raw_max_reviews not in (None, "") else 0
        )
        # CAN 水位：已请求的复盘次数（跨续跑不得清零，否则会超上限复盘）
        goal_reviews_requested = int(data.get("goal_reviews_requested") or 0)

        raw_ticks = data.get("observe_max_ticks")
        if raw_ticks is None or raw_ticks == "":
            observe_max_ticks = _DEFAULT_OBSERVE_MAX_TICKS
        else:
            observe_max_ticks = max(1, int(raw_ticks))

        recovery_attempts = int(data.get("recovery_attempts") or 0)
        max_recovery_attempts = int(
            data.get("max_recovery_attempts") or _DEFAULT_MAX_RECOVERY_ATTEMPTS
        )
        raw_schema = data.get("checkpoint_schema_version")
        intent_valid_until_raw = data.get("intent_valid_until")
        raw_ttl = data.get("intent_ttl_seconds")
        explicit_ttl_seconds: int | None = None
        if raw_ttl is not None and raw_ttl != "":
            explicit_ttl_seconds = max(1, int(raw_ttl))
        # 与 enable_continue_as_new 分离：CAN 开关 ≠ 允许恢复执行危险动作
        # （Cursor 第一百八十七批：总开关默认关，失败关闭）
        recovery_enabled = _truthy_flag(data.get("recovery_enabled"))

        # 跨活动推进水位：跳数与上限（跨 CAN 续跑累加，不得清零）
        advance_hops = int(data.get("advance_hops") or 0)
        raw_max_hops = data.get("max_advance_hops")
        max_advance_hops = (
            max(0, int(raw_max_hops))
            if raw_max_hops not in (None, "")
            else _DEFAULT_MAX_ADVANCE_HOPS
        )
        # 已推进过的活动 id（跨 CAN 累加）。**必须携带**：否则 Kernel 在准入失败或
        # 活动回落 READY 时会把同一个 id 反复报为就绪，续跑变成自激空转
        # （单测实测：桩每次返回同一 READY 活动 → 打满 200 跳上限）。
        advanced_activity_ids = [
            str(x) for x in (data.get("advanced_activity_ids") or [])
        ]
        # 显式关断开关（缺省开）；关断时行为与合入前一致
        raw_enable_advance = data.get("enable_advance")
        enable_advance = (
            True if raw_enable_advance is None else _truthy_flag(raw_enable_advance)
        )

        # 有界重试门：旧历史无 marker → 逐字保留原行为（不传 retry_policy，即 Temporal 默认）
        retry_bounded = workflow.patched(_ACTIVITY_RETRY_BOUNDED_PATCH)
        # 跨活动推进门：旧历史无本 marker ⇒ 不得新增「末段 list + CAN」命令序列
        advance_on_ready = workflow.patched(_ADVANCE_ON_READY_PATCH)
        # 全类活动准入门：旧历史无本 marker ⇒ 维持「仅 PLAN/EXECUTE」的既有命令序列
        admit_all_kinds = workflow.patched(_ADMIT_ALL_KINDS_PATCH)
        runner_authority_queues_patched = workflow.patched(
            _RUNNER_AUTHORITY_QUEUES_PATCH
        )
        runner_authority_queues = (
            runner_authority_queues_patched
            and _truthy_flag(data.get("runner_authority_queues"))
        )
        kernel_io_options: dict[str, Any] = {}
        if retry_bounded:
            kernel_io_options["retry_policy"] = _bounded_retry_policy(
                maximum_attempts=_KERNEL_IO_MAX_ATTEMPTS
            )

        # ensure 幂等 ACK：始终调用（已消费 command 亦安全）
        delivery = await workflow.execute_activity(
            "ensure_goal_delivery",
            args=[command_id, goal_id],
            start_to_close_timeout=timedelta(seconds=60),
            **kernel_io_options,
        )

        # Issue #24：放弃裁决必须先**持久化**再结束工作流。
        # 为什么：直接 return 只结束工作流，不在 PG 留痕；而 workflow_id_for_goal 是
        # 每 Goal 一个 ID → 重复驱动被去重挡回、不会重启 → Goal 静默永久卡住，
        # 与「健康但缓慢」不可区分。patch 门保护旧历史（无 marker 时维持旧行为）。
        abandon_persist = workflow.patched(_ABANDON_PERSISTENCE_PATCH)

        async def _abandon(reason: str) -> dict[str, Any]:
            """先落库放弃事实，再返回放弃摘要 —— 顺序不可颠倒。

            若落库失败则**整体失败**（异常冒泡）：宁可让工作流进入 Temporal 的重试/失败
            可见态，也不接受「工作流结束了但没有任何记录」—— 那正是本 Issue 要消除的静默态。
            """
            if abandon_persist:
                await workflow.execute_activity(
                    "record_recovery_abandonment",
                    args=[goal_id, reason, generation, prior_run_id],
                    start_to_close_timeout=timedelta(seconds=30),
                    **kernel_io_options,
                )
            return _recovery_abandon_result(
                command_id=command_id,
                goal_id=goal_id,
                generation=generation,
                prior_run_id=prior_run_id,
                reason=reason,
                delivery=dict(delivery) if isinstance(delivery, dict) else {},
            )

        # 预算读数：整数剩余量，无日期运算 → 合同允许的极大 wall_clock 不会溢出。
        delivery_budget_remaining: int | None = None
        budget_exhausted = False
        budget_unknown = False
        if isinstance(delivery, dict):
            raw_remaining = delivery.get("budget_remaining_wall_seconds")
            if isinstance(raw_remaining, int) and not isinstance(raw_remaining, bool):
                delivery_budget_remaining = max(0, raw_remaining)
            budget_exhausted = bool(delivery.get("budget_exhausted"))
            # 仅当字段**存在**且为 UNKNOWN 才失败关闭；
            # 老历史无该键 → 不得据此放弃（重放安全）。
            budget_unknown = delivery.get("budget_status") == "UNKNOWN"

        # 读数不可信（缺失/非法）→ 失败关闭，不得当作"消耗为 0"授予足额窗口。
        if budget_unknown:
            return await _abandon("BUDGET_READING_UNKNOWN")

        # 窗口优先级（裁定 §门3）。老历史的 delivery 无预算字段 → 逐字退回旧行为。
        intent_ttl_seconds = _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=explicit_ttl_seconds,
            budget_remaining_seconds=delivery_budget_remaining,
        )

        # 预算耗尽：失败关闭，任何新 admit/RunActivation 之前退出（Codex P1-3）。
        # 不再授予 1 秒窗口"碰运气"——耗尽即不可恢复。
        if budget_exhausted:
            return await _abandon("WALL_CLOCK_BUDGET_EXHAUSTED")

        # 门3：仅 patch 打开后对 generation>0 做恢复闸；旧历史无 marker 保持原命令序列
        recovery_safety = workflow.patched(_RECOVERY_SAFETY_GATE_PATCH)
        recovery_switch = workflow.patched(_RECOVERY_ENABLED_SWITCH_PATCH)
        if recovery_safety and generation > 0:
            abandon_reason: str | None = None
            # 总开关默认关：generation>0 且未显式 recovery_enabled → 放弃（0 admit / 0 RunActivation）
            if recovery_switch and not recovery_enabled:
                abandon_reason = "RECOVERY_DISABLED"
            elif raw_schema is None or int(raw_schema) != _RECOVERY_CHECKPOINT_SCHEMA_VERSION:
                abandon_reason = "CHECKPOINT_SCHEMA_INCOMPATIBLE"
            elif recovery_attempts > max_recovery_attempts:
                abandon_reason = "RECOVERY_ATTEMPTS_EXCEEDED"
            else:
                try:
                    deadline = _parse_intent_deadline(intent_valid_until_raw)
                except ValueError:
                    deadline = None
                    abandon_reason = "CHECKPOINT_SCHEMA_INCOMPATIBLE"
                if abandon_reason is None and (
                    deadline is None or _workflow_now_utc() > deadline
                ):
                    abandon_reason = "INTENT_EXPIRED"
            if abandon_reason is not None:
                return await _abandon(abandon_reason)

        actions_result = await workflow.execute_activity(
            "list_runtime_actions",
            args=[goal_id, owner_epoch],
            start_to_close_timeout=timedelta(seconds=60),
            **kernel_io_options,
        )
        action_ids: list[str] = []
        admitted_activity_ids: list[str] = []
        admitted_attempt_ids: list[str] = list(carried_attempt_ids)
        run_activation_results: list[dict[str, Any]] = []
        # 本轮需观察：新 admit 与 skip_admit（仅观察）合并
        # b086 patch：旧历史无 marker 时保持原 retry / CAN 观察命令序列。
        b086_recovery = workflow.patched("b086-temporal-recovery-v1")
        # M3 EXECUTE：旧历史 list 若含 EXECUTE 曾被忽略；无本 patch 不得新增命令。
        m3_execute = workflow.patched(_M3_EXECUTE_ORCHESTRATION_PATCH)
        # 激活预算 patch：旧历史无 marker → 逐字沿用钉扎 360s（活动超时属命令属性，须与 history 一致）
        activation_budget_patched = workflow.patched(_ACTIVATION_BUDGET_PATCH)
        # CAN 载荷透传预算：marker 判定必须在稳定落点无条件调用（可重放）
        can_budget_carry = workflow.patched(_CAN_BUDGET_CARRY_PATCH)
        # CAN 续跑载荷的预算透传字段：patch 门内才携带（旧历史重放保持逐字一致）
        can_extra: dict[str, Any] = {}
        if can_budget_carry:
            can_extra["activation_budget_seconds"] = data.get(
                "activation_budget_seconds"
            )

        # 周期复盘门：无本 marker 的旧历史**逐字沿用**（不产生任何新命令，可重放）
        goal_review_patched = workflow.patched(_GOAL_REVIEW_PATCH)
        # 同一位置无条件调用，保证 marker 落点稳定（可重放）
        goal_review_seq_base_patched = workflow.patched(_GOAL_REVIEW_SEQ_BASE_PATCH)
        # 三重失败关闭：无门 / 间隔未配或过小 / 上限未配 → 一律不触发
        goal_review_enabled = (
            goal_review_patched
            and goal_review_interval_seconds >= _GOAL_REVIEW_MIN_INTERVAL_SECONDS
            and max_goal_reviews > 0
            and goal_reviews_requested < max_goal_reviews
        )
        next_goal_review_at = (
            workflow.now() + timedelta(seconds=goal_review_interval_seconds)
            if goal_review_enabled
            else None
        )
        goal_reviews_created: list[dict[str, Any]] = []
        goal_review_notes: list[str] = []
        # Continue-As-New 后 Kernel 只会返回 READY；已经准入的 RUNNING Activity
        # 必须从持久水位恢复观察，不能依赖下一次动作列表再次出现。
        # 激活预算未采用显式值时的原因（误配可见；为空表示无冲突）
        activation_budget_notes: list[str] = []
        # 观察到「租约已丢」的活动（见 _ACTIVITY_LEASE_LOST_STATUS）；让该失败模式**可见**
        lease_lost_activity_ids: list[str] = []
        observe_activity_ids: list[str] = (
            list(skip_admit_activity_ids) if b086_recovery else []
        )

        for action in actions_result.get("actions") or []:
            activity_id = str(action.get("activity_id") or "")
            if not activity_id:
                continue
            action_ids.append(activity_id)
            kind = str(action.get("kind") or "")
            # 三档（逐层放宽，全部 patch 门控以保护旧历史命令序列）：
            #   旧历史        → 仅 PLAN
            #   m3_execute    → PLAN + EXECUTE
            #   admit_all_kinds → 五类（Kernel 在 EXECUTE 成功后自动创建 AUDIT /
            #                     INTEGRATE / FINALIZE；不认这三类则目标到不了 DONE）
            if admit_all_kinds:
                if kind not in ("PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE"):
                    continue
            elif m3_execute:
                if kind not in ("PLAN", "EXECUTE"):
                    continue
            elif kind != "PLAN":
                continue

            if activity_id in skip_admit_set:
                # 已 admit：跳过 admit + RunActivation，仅后续观察
                continue

            # 确定性幂等键：replay 安全
            admit_key = f"goal-wf-admit:{activity_id}"
            if admit_all_kinds and kind not in ("PLAN", "EXECUTE"):
                admit_name = "admit_runtime_action"
                admit_args: list[str] = [activity_id, admit_key, kind]
            else:
                admit_name = (
                    "admit_plan_action" if kind == "PLAN" else "admit_execute_action"
                )
                admit_args = [activity_id, admit_key]
            lease = await workflow.execute_activity(
                admit_name,
                args=admit_args,
                start_to_close_timeout=timedelta(seconds=60),
                **kernel_io_options,
            )
            admitted_activity_ids.append(str(lease.get("activity_id") or activity_id))
            attempt_id = lease.get("attempt_id")
            if attempt_id:
                admitted_attempt_ids.append(str(attempt_id))
            observe_activity_ids.append(activity_id)

            # 字面量 Activity 名与队列，避免沙箱 import orchestration.client
            # 预算：旧历史逐字 360s；新历史可经 payload 放大（只许放大）。
            # 用**不抛错**的版本：工作流内 raise 会让 Workflow Task 反复失败重试、
            # 工作流永久挂住（本批实测），故以「安全退回 + 回报原因」表达冲突。
            if activation_budget_patched:
                budget_seconds, budget_note = _effective_activation_budget_seconds(
                    data.get("activation_budget_seconds"), observe_max_ticks=observe_max_ticks
                )
                if budget_note:
                    activation_budget_notes.append(budget_note)
            else:
                budget_seconds = _DEFAULT_ACTIVATION_BUDGET_SECONDS
            activation_options: dict[str, Any] = {
                "task_queue": (
                    _runner_task_queue_for_kind(kind)
                    if runner_authority_queues
                    else _LEGACY_RUNNER_TASK_QUEUE
                ),
                "start_to_close_timeout": timedelta(seconds=budget_seconds),
            }
            if b086_recovery:
                # 模型 activation 可能产生费用或外部动作。技术层不自动重跑；
                # 失败后由下方 Kernel 状态观察核对业务结果。
                activation_options["retry_policy"] = RetryPolicy(maximum_attempts=1)
            try:
                act_result = await workflow.execute_activity(
                    "RunActivation",
                    args=[
                        {
                            "activity_id": activity_id,
                            "attempt_id": str(lease.get("attempt_id") or ""),
                            "goal_id": goal_id,
                            "kind": kind,
                            "fencing_epoch": str(lease.get("fencing_epoch") or ""),
                            "owner_epoch": owner_epoch,
                        }
                    ],
                    **activation_options,
                )
            except ActivityError as exc:
                # 旧历史保持原失败语义；取消必须向上冒泡，由停止流程处理。
                if not b086_recovery or is_cancelled_exception(exc):
                    raise
                # Temporal Activity 失败不是业务失败结论；继续经 Kernel 观察。
                # 不携带异常正文，避免把 SDK/模型细节写入 history。
                act_result = {
                    "status": "FAILED",
                    "reason": "RunActivation失败，转入Kernel状态核对",
                    "kind": kind,
                    "pending_harness": False,
                    "activity_id": activity_id,
                    "error_type": type(exc).__name__,
                }
            # NOT_IMPLEMENTED / pending_harness 是诚实中间态，不使 Workflow 失败
            if isinstance(act_result, dict):
                run_activation_results.append(act_result)
            else:
                run_activation_results.append({"result": act_result})

        pending_harness = any(
            bool(r.get("pending_harness")) or r.get("status") == "NOT_IMPLEMENTED"
            for r in run_activation_results
        )

        # RunActivation 之后观察库内终态；pending_harness 仍观察（可能 OBSERVE_TIMEOUT）
        # 字段名 plan_terminal_statuses 为历史兼容；m3 patch 下可含 EXECUTE。
        plan_terminal_statuses: list[dict[str, str]] = []
        # 退避门：无本 marker 的旧历史逐字沿用固定 1s（定时器是命令，改动会破坏重放）
        observe_backoff = workflow.patched(_OBSERVE_BACKOFF_PATCH)
        for activity_id in observe_activity_ids:
            recorded = "OBSERVE_TIMEOUT"
            for tick in range(observe_max_ticks):
                # 与 carried 分支对齐：单次尝试，重试交由本循环的**有界退避**承担
                # （否则活动内无限重试会让 observe_max_ticks 永不耗尽）
                observe_options: dict[str, Any] = {}
                if retry_bounded:
                    observe_options["retry_policy"] = _bounded_retry_policy(
                        maximum_attempts=_OBSERVE_MAX_ATTEMPTS
                    )
                try:
                    if b086_recovery and activity_id in skip_admit_set:
                        # PLAN 专用入口保留；m3 起用 activation 观察（含 EXECUTE）
                        carried_name = (
                            "observe_carried_activation_activity_status"
                            if m3_execute
                            else "observe_carried_plan_activity_status"
                        )
                        observed = await workflow.execute_activity(
                            carried_name,
                            args=[goal_id, owner_epoch, activity_id],
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        )
                    else:
                        observed = await workflow.execute_activity(
                            "observe_activity_status",
                            args=[activity_id],
                            start_to_close_timeout=timedelta(seconds=30),
                            **observe_options,
                        )
                except ActivityError:
                    # 观察本身失败（技术错误）→ 记为本轮"未得终态"，交由下方有界退避进入下一轮；
                    # 轮次耗尽即 OBSERVE_TIMEOUT，从而进入既有的有界恢复/放弃路径。
                    # 不这样做则异常冒泡杀死工作流：既不到 OBSERVE_TIMEOUT，也不推进 recovery_attempts。
                    # 旧历史（无 retry_bounded 门）逐字保持原行为：异常照常冒泡。
                    if not retry_bounded:
                        raise
                else:
                    status = str(observed.get("status") or "")
                    if status in _ACTIVATION_TERMINAL:
                        recorded = status
                        break
                    if status == _ACTIVITY_LEASE_LOST_STATUS:
                        # 失租信号：租约过期时 expire_stale_leases 把活动置 RECOVERING（**非终态**）。
                        # 不显式记录的话，工作流只能看到「非终态」，**无法与「正常推进」区分**，
                        # 会一路观察到 OBSERVE_TIMEOUT（默认 ≈24min）才开始恢复 ——
                        # 而实际工作早已作废（effect 全被拒、资源/预算已 QUARANTINED）。
                        # 实测代价：官方多步循环 4 轮 / 3 个工具调用全被 409 拒绝，
                        # 从外部看却像「模型没产出工具调用」，排查绕远。
                        # 仅记录、**不改控制流**（改控制流会动命令序列，须另开 patch 门）。
                        lease_lost_activity_ids.append(activity_id)
                # 末轮不必再 sleep；time-skipping 友好
                if tick + 1 < observe_max_ticks:
                    delay_seconds = (
                        _observe_poll_delay_seconds(tick) if observe_backoff else 1
                    )
                    await workflow.sleep(timedelta(seconds=delay_seconds))

                # 周期 GoalReview：到点即请求一次（幂等键含 seq，Kernel 去重）
                if (
                    goal_review_enabled
                    and next_goal_review_at is not None
                    and workflow.now() >= next_goal_review_at
                    and goal_reviews_requested < max_goal_reviews
                ):
                    # **契约**：Kernel 要求 review_seq ≥ 1（0 会被判 GOAL_REVIEW_TRIGGER_INVALID）。
                    # 内部计数保持 0 基（便于与 max_goal_reviews 比较），外发时 +1；
                    # trigger_key 与所发 seq 同源，便于按 key 对账。
                    # 旧历史（无本 patch 标记）记录的是 0 基 seq ⇒ 走旧分支逐字重放。
                    review_seq = (
                        goal_reviews_requested + 1
                        if goal_review_seq_base_patched
                        else goal_reviews_requested
                    )
                    try:
                        review = await workflow.execute_activity(
                            "request_goal_review",
                            args=[goal_id, f"{goal_id}:{review_seq}", review_seq],
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        )
                    except ActivityError as exc:
                        # 接线/基础设施故障：**不推进计数**（避免"失败也算复盘过"），
                        # 记原因供排查；下个 tick 再试。
                        goal_review_notes.append(
                            f"request_failed:seq={review_seq}:{type(exc).__name__}"
                        )
                    else:
                        if review.get("rejected"):
                            # 设计内拒绝（未停滞 / 间隔过短 / 预算耗尽）：失败关闭成立。
                            # 不推进计数，但**按原因码分流**——否则每 tick 撞同一面墙、
                            # 把日志刷满同一条拒绝，掩盖真实进展。
                            reason = str(review.get("reason_code") or "")
                            goal_review_notes.append(
                                f"rejected:seq={review_seq}:{reason}"
                            )
                            if reason == "GOAL_REVIEW_BUDGET_EXHAUSTED":
                                # 终局：预算耗尽后不再请求（Kernel 仍会兜底拒绝）
                                goal_review_enabled = False
                        else:
                            goal_reviews_requested = review_seq
                            goal_reviews_created.append(
                                {
                                    "seq": review_seq,
                                    "activity_id": str(review.get("activity_id") or ""),
                                    "created": bool(review.get("created")),
                                    "reviews_remaining": int(
                                        review.get("reviews_remaining") or 0
                                    ),
                                }
                            )
                            if int(review.get("reviews_remaining") or 0) <= 0:
                                # 额度用尽：同样停止请求，避免无谓的拒绝往返
                                goal_review_enabled = False
                    next_goal_review_at = workflow.now() + timedelta(
                        seconds=goal_review_interval_seconds
                    )
            plan_terminal_statuses.append(
                {"activity_id": activity_id, "status": recorded}
            )

        # observe-timeout 恢复：Continue-As-New 携带水位，禁止清零已消费命令
        any_observe_timeout = any(
            row.get("status") == "OBSERVE_TIMEOUT" for row in plan_terminal_statuses
        )
        if (
            any_observe_timeout
            and enable_continue_as_new
            and continue_as_new_on_observe_timeout
        ):
            next_skip = sorted(set(skip_admit_activity_ids) | set(admitted_activity_ids))
            next_consumed = sorted(set(command_ids_consumed) | {command_id})
            # 计数在续跑危险动作之前经 CAN payload 落盘（门3：崩溃不得清零尝试数）
            next_attempts = recovery_attempts + 1
            if intent_valid_until_raw:
                next_valid_until = str(intent_valid_until_raw)
            else:
                next_valid_until = (
                    workflow.now() + timedelta(seconds=intent_ttl_seconds)
                ).isoformat()
            workflow.continue_as_new(
                {
                    "command_id": command_id,
                    "goal_id": goal_id,
                    "owner_epoch": owner_epoch,
                    "command_ids_consumed": next_consumed,
                    "skip_admit_activity_ids": next_skip,
                    "admitted_attempt_ids": sorted(set(admitted_attempt_ids)),
                    "generation": generation + 1,
                    "prior_run_id": workflow.info().run_id,
                    "observe_max_ticks": observe_max_ticks,
                    "enable_continue_as_new": True,
                    "continue_as_new_on_observe_timeout": True,
                    "checkpoint_schema_version": _RECOVERY_CHECKPOINT_SCHEMA_VERSION,
                    "recovery_attempts": next_attempts,
                    "max_recovery_attempts": max_recovery_attempts,
                    "intent_valid_until": next_valid_until,
                    "intent_ttl_seconds": intent_ttl_seconds,
                    # CAN 续跑显式打开恢复；与仅开 enable_continue_as_new 的外部入口区分
                    "recovery_enabled": True,
                    # 复盘水位随续跑落盘：清零会绕过 max_goal_reviews 上限
                    "goal_reviews_requested": goal_reviews_requested,
                    "runner_authority_queues": runner_authority_queues,
                    **can_extra,
                }
            )

        # 跨活动推进：本轮有**实质进展**且 Kernel 仍报有 READY 工作 → Continue-As-New 续跑。
        #
        # 为什么需要（2026-09-15 实测）：本工作流取一次 READY 动作后就 return，而新 run 只由
        # ENSURE_WORKFLOW 投递触发、该投递全仓仅在 START 入队 ⇒ PLAN 成功后新 READY 的
        # EXECUTE 无人推进，Goal 静默停在 READY（实测二十余分钟无变化）。
        #
        # 为何用 CAN 而非「入队 + 起新 run」：CAN 是同一条工作流的续跑，不会与自身并发；
        # 起新 run 会撞同一 workflow_id（ALLOW_DUPLICATE 下会静默重叠）。
        #
        # 为何要「有实质进展」这个前置：若 Kernel 报有 READY 但准入一直失败（如资源不足），
        # 无进展也续跑会变成高速空转。无进展即**不续跑**，交回外部驱动。
        advance_note: str | None = None
        if advance_on_ready and enable_advance:
            progressed = bool(admitted_activity_ids) or any(
                row.get("status") in _ACTIVATION_TERMINAL
                for row in plan_terminal_statuses
            )
            if not progressed:
                advance_note = "NO_PROGRESS_THIS_RUN"
            elif advance_hops >= max_advance_hops:
                # 兜底失败关闭：达到跳数上限即停，交回外部驱动
                advance_note = f"ADVANCE_HOPS_EXHAUSTED:{advance_hops}"
            else:
                more = await workflow.execute_activity(
                    "list_runtime_actions",
                    args=[goal_id, owner_epoch],
                    start_to_close_timeout=timedelta(seconds=60),
                    **kernel_io_options,
                )
                # 就绪过滤必须与准入的 kind 面**一致**：只认 PLAN/EXECUTE 时，
                # 若 Kernel 只报 AUDIT 就绪（EXECUTE 成功后的常态），这里会误判
                # 「无就绪工作」而收束 —— 正是本补丁要消除的静默停滞。
                ready_kinds_allowed = (
                    ("PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE")
                    if admit_all_kinds
                    else ("PLAN", "EXECUTE")
                )
                ready_ids = {
                    str(a.get("activity_id") or "")
                    for a in (more.get("actions") or [])
                    if str(a.get("kind") or "") in ready_kinds_allowed
                    and str(a.get("activity_id") or "")
                }
                # **只有出现「尚未推进过」的就绪活动才续跑**。这是防自激的关键守卫：
                # 若 Kernel 把刚准入过的活动又报为 READY（准入失败 / 活动回落），
                # 或桩恒返回同一活动，无此守卫会一路空转到跳数上限。
                # 排除面必须同时含**本 run 刚准入**的 id：否则本 run 准入的活动会被
                # 自己判成「新的」，白续跑一跳（单测实测：多出一次重复 admit）。
                handled_ids = set(advanced_activity_ids) | set(admitted_activity_ids)
                fresh_ids = ready_ids - handled_ids
                if not ready_ids:
                    # Kernel 已无就绪工作：正常收束（Goal 可能已终态或待人工验收）
                    advance_note = "NO_READY_WORK"
                elif not fresh_ids:
                    # 就绪活动都已推进过（重复报到）→ 不续跑，交回外部驱动
                    advance_note = "READY_ALREADY_ADVANCED"
                else:
                    # 水位随续跑落盘：generation / recovery_* **逐字沿用**，不得自增 ——
                    # 本续跑不是恢复；自增会触发恢复安全门把 Goal 误判为待恢复而放弃。
                    workflow.continue_as_new(
                        {
                            "command_id": command_id,
                            "goal_id": goal_id,
                            "owner_epoch": owner_epoch,
                            "command_ids_consumed": sorted(
                                set(command_ids_consumed) | {command_id}
                            ),
                            "skip_admit_activity_ids": list(skip_admit_activity_ids),
                            "admitted_attempt_ids": list(carried_attempt_ids),
                            "generation": generation,
                            "prior_run_id": workflow.info().run_id,
                            "observe_max_ticks": observe_max_ticks,
                            "enable_continue_as_new": enable_continue_as_new,
                            "continue_as_new_on_observe_timeout": (
                                continue_as_new_on_observe_timeout
                            ),
                            "checkpoint_schema_version": raw_schema,
                            "recovery_attempts": recovery_attempts,
                            "max_recovery_attempts": max_recovery_attempts,
                            "intent_valid_until": intent_valid_until_raw,
                            "intent_ttl_seconds": explicit_ttl_seconds,
                            "recovery_enabled": recovery_enabled,
                            "goal_reviews_requested": goal_reviews_requested,
                            "runner_authority_queues": runner_authority_queues,
                            # 跳数自增：跨续跑累加，不得清零（否则上限形同虚设）
                            "advance_hops": advance_hops + 1,
                            "max_advance_hops": max_advance_hops,
                            # 已推进活动 id：**必须有界**。无界累加会把载荷撑爆
                            # （实测：打到跳数上限时 200 条 id ⇒ 单条载荷 8457 字符，
                            #  触发 temporal-history-v1 的「单条 payload < 4096」门禁）。
                            # 只保留最近若干条即可防「同一活动被反复推进」——
                            # Kernel 侧的就绪工作以最近邻为主，历史条目无需常驻。
                            "advanced_activity_ids": sorted(
                                set(advanced_activity_ids) | set(admitted_activity_ids)
                            )[-_ADVANCE_TRACKED_IDS_MAX:],
                            **can_extra,
                        }
                    )

        # 只回传 id / pending / 观察摘要；绝不在此标记 Goal DONE
        return {
            "ok": True,
            "command_id": str(delivery.get("command_id") or command_id),
            "workflow_id": delivery.get("workflow_id"),
            "run_id": delivery.get("run_id"),
            "delivery_status": delivery.get("delivery_status"),
            "action_ids": action_ids,
            "admitted_activity_ids": admitted_activity_ids,
            "admitted_attempt_ids": admitted_attempt_ids,
            "run_activation_results": run_activation_results,
            "activation_budget_notes": activation_budget_notes,
            "goal_reviews_created": goal_reviews_created,
            "goal_reviews_requested": goal_reviews_requested,
            "goal_review_notes": goal_review_notes,
            "lease_lost_activity_ids": sorted(set(lease_lost_activity_ids)),
            "pending_harness": pending_harness,
            "plan_terminal_statuses": plan_terminal_statuses,
            "generation": generation,
            "prior_run_id": prior_run_id,
            "command_ids_consumed": list(command_ids_consumed),
            "continued_as_new": False,
            # 跨活动推进决策：None 表示本 run 未启用该门；有值即为何未续跑（可见即可排查）。
            # 真正续跑时不会走到这里（CAN 不返回）。
            "advance_note": advance_note,
            "advance_hops": advance_hops,
            "marks_goal_done": False,
        }
