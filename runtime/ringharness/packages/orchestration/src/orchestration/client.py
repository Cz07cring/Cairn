"""可注入 Temporal 客户端：Fake 默认；RING_TEMPORAL_TARGET 时可用 Real。

不得在 Temporal 不可用时回退 LEGACY；ensure_started 失败抛 TemporalUnavailable。

**已关闭的 Workflow 不是「不可用」**：它抛 TemporalWorkflowClosed（见该类 docstring），
以免把「命令投递到死 run」伪装成成功。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Protocol
from uuid import uuid4

# 本机 Worker / Relay 共用的控制队列名（与 deploy/temporal、workflow-worker 一致）
DEFAULT_TASK_QUEUE = "ring-control"
# TS Runner Activity 队列（与 ring-control 分离；GoalWorkflow 派发 RunActivation）
RUNNER_TASK_QUEUE = "ring-runner"
MANAGER_RUNNER_TASK_QUEUE = "ring-runner-manager"
EXECUTOR_RUNNER_TASK_QUEUE = "ring-runner-executor"
AUDITOR_RUNNER_TASK_QUEUE = "ring-runner-auditor"
# 具名 Activity；Workflow 内用字面量，避免沙箱 import
RUN_ACTIVATION_ACTIVITY_NAME = "RunActivation"

# 可复用 run 的 Workflow 状态：只有仍在运行才谈得上「命令会跑到」。
# 其余状态（COMPLETED/FAILED/CANCELED/TERMINATED/TIMED_OUT/CONTINUED_AS_NEW）
# 一律视为已关闭 —— 见 _require_open_run。
_RUN_OPEN_STATUSES = frozenset({"RUNNING"})


# GoalWorkflow 启动载荷可选键白名单（单一事实来源）。
# 新增旋钮必须登记在此，否则调用方传了也会被静默丢弃
# （本仓 AGENTS §17 同族坑：白名单漏登记 = 功能不可达）；
# tests/temporal/test_tm12_history_hygiene.py 的 payload 卫生门直接引用本常量。
_GOAL_WORKFLOW_OPTIONAL_KEYS = (
    "observe_max_ticks",
    "enable_continue_as_new",
    "continue_as_new_on_observe_timeout",
    "command_ids_consumed",
    "skip_admit_activity_ids",
    "admitted_attempt_ids",
    "generation",
    "prior_run_id",
    "checkpoint_schema_version",
    "recovery_attempts",
    "max_recovery_attempts",
    "intent_valid_until",
    "intent_ttl_seconds",
    "recovery_enabled",
    # RunActivation 派发预算：**必须登记在此**，否则调用方传了也会被静默丢弃
    "activation_budget_seconds",
    # 周期 GoalReview（计时归工作流、去重归 Kernel）—— 同属硬边界，必须登记
    "goal_review_interval_seconds",
    "max_goal_reviews",
    # 复盘水位：CAN 续跑入口需能显式携带（否则清零 → 超上限复盘）
    "goal_reviews_requested",
    # 跨活动推进（advance-on-ready）：**必须登记在此**，否则调用方传了也被静默丢弃
    "advance_hops",
    "max_advance_hops",
    "enable_advance",
    "advanced_activity_ids",
    # 新 Workflow 默认进入分权 Runner 队列；旧 history 输入无此字段，继续走 legacy 队列。
    "runner_authority_queues",
)


def build_goal_workflow_payload(
    kwargs: dict, env: dict | None = None
) -> dict:
    """构造 GoalWorkflow 启动载荷 —— 纯函数，便于单测（不连 Temporal、不读全局 env）。

    **为什么必须集中在这里**：可选键是**白名单**透传的（`if key in kwargs`），
    新增一个旋钮却忘记登记，调用方传了也会被**静默丢弃** —— 表现为「功能已实现但不可达」。
    本函数把该白名单变成可单测的单一事实来源。

    环境变量兜底（未显式传 kwargs 时生效），供生产运维在不改代码的前提下调整：
      - RING_GOAL_WORKFLOW_OBSERVE_MAX_TICKS      观察轮次上限
      - RING_GOAL_WORKFLOW_ENABLE_CONTINUE_AS_NEW 开启 CAN 水位续跑
      - RING_GOAL_WORKFLOW_RECOVERY_ENABLED       恢复总开关（与 CAN 分离，默认关）
      - RING_GOAL_WORKFLOW_ACTIVATION_BUDGET_SECONDS  RunActivation 派发预算（秒，只许放大）
      - RING_GOAL_WORKFLOW_GOAL_REVIEW_INTERVAL_SECONDS  周期 GoalReview 间隔（秒）
      - RING_GOAL_WORKFLOW_MAX_GOAL_REVIEWS              复盘次数上限（防无限复盘）
    """
    source = os.environ if env is None else env
    payload: dict = {
        "command_id": str(kwargs.get("command_id") or ""),
        "goal_id": str(kwargs.get("goal_id") or ""),
        "owner_epoch": str(kwargs.get("owner_epoch") or "1"),
    }
    # 可选水位 / 观察控制 / 恢复安全策略（测试与 env→payload 共用）
    for key in _GOAL_WORKFLOW_OPTIONAL_KEYS:
        if key in kwargs and kwargs[key] is not None:
            payload[key] = kwargs[key]
    # 环境变量兜底：生产可开 CAN / 调观察轮次 / 放大激活预算（未传 kwargs 时）
    if "observe_max_ticks" not in payload:
        raw_ticks = (source.get("RING_GOAL_WORKFLOW_OBSERVE_MAX_TICKS") or "").strip()
        if raw_ticks:
            payload["observe_max_ticks"] = int(raw_ticks)
    if "activation_budget_seconds" not in payload:
        raw_budget = (
            source.get("RING_GOAL_WORKFLOW_ACTIVATION_BUDGET_SECONDS") or ""
        ).strip()
        if raw_budget:
            payload["activation_budget_seconds"] = int(raw_budget)
    if "goal_review_interval_seconds" not in payload:
        raw_interval = (
            source.get("RING_GOAL_WORKFLOW_GOAL_REVIEW_INTERVAL_SECONDS") or ""
        ).strip()
        if raw_interval:
            payload["goal_review_interval_seconds"] = int(raw_interval)
    if "max_goal_reviews" not in payload:
        raw_max_reviews = (source.get("RING_GOAL_WORKFLOW_MAX_GOAL_REVIEWS") or "").strip()
        if raw_max_reviews:
            payload["max_goal_reviews"] = int(raw_max_reviews)
    if "enable_continue_as_new" not in payload:
        raw_can = (
            source.get("RING_GOAL_WORKFLOW_ENABLE_CONTINUE_AS_NEW") or ""
        ).strip().lower()
        if raw_can in ("1", "true", "yes"):
            payload["enable_continue_as_new"] = True
    # 恢复总开关与 CAN 分离；仅显式 env/kwargs 打开（默认关）
    if "recovery_enabled" not in payload:
        raw_rec = (
            source.get("RING_GOAL_WORKFLOW_RECOVERY_ENABLED") or ""
        ).strip().lower()
        if raw_rec in ("1", "true", "yes"):
            payload["recovery_enabled"] = True
    # 跨活动推进：缺省开（缺它则 PLAN 成功后新 READY 的 EXECUTE 无人推进）；
    # 显式 env 可关断，便于生产在异常自激时止血。
    if "enable_advance" not in payload:
        raw_adv = (source.get("RING_GOAL_WORKFLOW_ENABLE_ADVANCE") or "").strip().lower()
        if raw_adv in ("0", "false", "no"):
            payload["enable_advance"] = False
    if "max_advance_hops" not in payload:
        raw_max_adv = (source.get("RING_GOAL_WORKFLOW_MAX_ADVANCE_HOPS") or "").strip()
        if raw_max_adv:
            payload["max_advance_hops"] = int(raw_max_adv)
    if "runner_authority_queues" not in payload:
        raw_authority_queues = (
            source.get("RING_GOAL_WORKFLOW_RUNNER_AUTHORITY_QUEUES") or "1"
        ).strip().lower()
        payload["runner_authority_queues"] = raw_authority_queues not in (
            "0",
            "false",
            "no",
        )
    return payload


class TemporalUnavailable(Exception):
    """编排服务不可用；投递保持 PENDING，禁止回退 LEGACY。

    **与 TemporalWorkflowClosed 不同**：本异常表示「现在不行、稍后可能行」，
    调用方保持 PENDING 重试即可；已关闭则重试**永远**无用。
    """

    def __init__(self, message: str = "Temporal 不可用") -> None:
        self.message = message
        super().__init__(message)


class TemporalWorkflowClosed(Exception):
    """未提供可核对命令身份时，目标 Workflow 已关闭，不能复用其运行权。

    **确证的卡死成因（本批修复）**：`RealTemporalClient` 的**进程内缓存**
    (`self._runs`) 命中即返回，无活性校验 —— 长驻进程（control API）会把已关闭的
    run_id 反复交回调用方，调用方 `relay.ensure_workflow` 随即**无条件** ACK，
    于是不同命令可能被误记为「已投递」却指向永不执行的 run。

    启动显式使用 `REJECT_DUPLICATE`：旧 run 即使已关闭也不会被静默重开。

    ACK 丢失时可用 Temporal 历史中的启动载荷核对同一命令，再补 ACK；
    不会重新启动同 ID run。新重规划使用新的 workflow_id 与 owner_epoch。
    """

    def __init__(self, *, workflow_id: str, run_id: str, status_name: str) -> None:
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.status_name = status_name
        self.message = (
            f"Workflow 已关闭（{status_name}），不得投递："
            f"workflow_id={workflow_id} run_id={run_id}"
        )
        super().__init__(self.message)


def _status_name(status: object) -> str:
    """把 Temporal 状态（枚举或字符串）归一为名字，供纯函数判定。"""
    name = getattr(status, "name", None)
    return str(name) if name else str(status)


def _require_open_run(workflow_id: str, *, run_id: str, status_name: str) -> str:
    """已存在 Workflow 时，决定「复用其 run_id」还是**失败关闭**。

    存在的 Workflow 分两类，**必须区分**（此前不区分 → 静默投递到死 run）：

      - RUNNING  → 幂等复用其 run_id（正确：命令确实会跑到）
      - 已关闭   → 抛 `TemporalWorkflowClosed`，让调用方显式处理

    启动使用 `REJECT_DUPLICATE`，已有 run 均走 AlreadyStarted → describe；
    无命令身份时仅 RUNNING 可复用；带命令身份时由调用方检查历史载荷。

    纯函数：不连 Temporal、不读全局，便于对全部状态穷举单测。
    """
    if status_name in _RUN_OPEN_STATUSES:
        return run_id
    raise TemporalWorkflowClosed(
        workflow_id=workflow_id, run_id=run_id, status_name=status_name
    )


class TemporalClient(Protocol):
    """与 FakeTemporalClient.ensure_started 兼容的窄接口。"""

    def ensure_started(self, workflow_id: str, **kwargs) -> str: ...


class FakeTemporalClient:
    """进程内假客户端：同 workflow_id 幂等返回同一 run_id。"""

    def __init__(self) -> None:
        self._runs: dict[str, str] = {}
        self.unavailable = False

    def ensure_started(self, workflow_id: str, **kwargs) -> str:
        if self.unavailable:
            raise TemporalUnavailable("Temporal 不可用")
        existing = self._runs.get(workflow_id)
        if existing is not None:
            return existing
        run_id = f"run-{uuid4()}"
        self._runs[workflow_id] = run_id
        return run_id


class RealTemporalClient:
    """薄封装：仅在 RING_TEMPORAL_TARGET 可连时使用 temporalio Client。

    与 FakeTemporalClient 同形 ensure_started；启动 GoalWorkflow
    （ensure→list→admit PLAN→RunActivation on ring-runner），
    不写 Goal DONE、不回退 LEGACY。
    """

    def __init__(
        self,
        target: str,
        *,
        namespace: str = "default",
        task_queue: str = DEFAULT_TASK_QUEUE,
    ) -> None:
        self._target = target.strip()
        if not self._target:
            raise ValueError("RealTemporalClient 需要非空 target")
        self._namespace = namespace
        self._task_queue = task_queue
        # 仅供进程内观察/诊断；**不再**作为幂等判据（见 ensure_started）
        self._runs: dict[str, str] = {}

    def ensure_started(self, workflow_id: str, **kwargs) -> str:
        """确保 GoalWorkflow 在跑；返回其 run_id。

        权威幂等判据是 Temporal 自身（start → AlreadyStarted → describe → 分流），
        **不再信任 `self._runs` 做短路返回**：该缓存无活性校验，长驻进程
        （如 control API）一旦命中就会把**已关闭**的 run_id 原样交回，
        调用方据此 ACK 一条永不执行的命令 ⇒ Goal 静默卡死（本批确证并修掉）。

        已关闭的同 ID run 不得静默重开；新重规划有独立 workflow_id。
        """
        try:
            run_id = asyncio.run(self._ensure_started_async(workflow_id, **kwargs))
        except (TemporalUnavailable, TemporalWorkflowClosed, ValueError):
            # 二者语义不同，均须原样上抛：前者重试有意义，后者重试永远无用。
            # 若在此被包成 TemporalUnavailable，调用方会把「已关闭」误读为「暂时不可用」。
            raise
        except Exception as exc:
            raise TemporalUnavailable(f"Temporal 不可用: {exc}") from exc
        self._runs[workflow_id] = run_id
        return run_id

    async def _ensure_started_async(self, workflow_id: str, **kwargs) -> str:
        from temporalio.client import Client
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from .temporal_workflows import GoalWorkflow

        namespace = str(kwargs.get("namespace") or self._namespace)
        task_queue = str(kwargs.get("task_queue") or self._task_queue)
        try:
            client = await Client.connect(self._target, namespace=namespace)
        except Exception as exc:
            raise TemporalUnavailable(f"无法连接 Temporal: {exc}") from exc

        payload = build_goal_workflow_payload(kwargs)
        try:
            handle = await client.start_workflow(
                GoalWorkflow.run,
                payload,
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return handle.result_run_id
        except WorkflowAlreadyStartedError:
            # start 成功但 ACK 丢失时，旧 run 可能已经结束。只有历史启动载荷
            # 与本次命令完全一致，才可补 ACK；任何情形都不得重新执行同 ID。
            handle = client.get_workflow_handle(workflow_id)
            desc = await handle.describe()
            command_id = kwargs.get("command_id")
            if command_id is not None:
                history = await handle.fetch_history()
                if not history.events:
                    raise ValueError("Temporal 历史为空，拒绝 ACK")
                started = history.events[0].workflow_execution_started_event_attributes
                decoded = await client.data_converter.decode(started.input.payloads)
                payload = decoded[0] if decoded else None
                if not isinstance(payload, dict) or any(
                    str(payload.get(key)) != str(kwargs.get(key))
                    for key in ("command_id", "goal_id", "owner_epoch")
                ):
                    raise ValueError("Temporal 启动身份与投递命令不一致，拒绝 ACK")
                return str(desc.run_id)
            return _require_open_run(
                workflow_id,
                run_id=str(desc.run_id),
                status_name=_status_name(desc.status),
            )


def build_temporal_client(environ: Mapping[str, str] | None = None) -> TemporalClient:
    """未设置 RING_TEMPORAL_TARGET 时返回 Fake；设置后构造 Real（连接延迟到 ensure_started）。"""
    env = os.environ if environ is None else environ
    target = (env.get("RING_TEMPORAL_TARGET") or "").strip()
    if not target:
        return FakeTemporalClient()
    namespace = (env.get("RING_TEMPORAL_NAMESPACE") or "default").strip() or "default"
    return RealTemporalClient(target=target, namespace=namespace)
