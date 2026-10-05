"""有界重试：消除 Temporal 默认的**无限重试**导致的静默挂死。

**缺陷背景**（本批修复）：
多处 `execute_activity` 未传 `retry_policy`。Temporal 的默认是
`maximum_attempts=0` = **无限重试**。后果是一条静默挂死链：

    持续失败的 Kernel Activity
      → 无限重试（永不判定失败）
      → 工作流永不到 OBSERVE_TIMEOUT
      → recovery_attempts 永不递增
      → RECOVERY_ABANDONED 永不发生
      → Goal 永久卡住，无错误、无告警

即：**无限重试绕过了整套有界恢复设计**（也与前一批修掉的观察窗口缺陷呼应 ——
窗口有界，但活动内无限重试会让窗口永不耗尽）。

规格依据：`doc/v0.6` §5.5「技术错误允许**有限**退避；业务失败进入 Kernel 决策」
以及「RunActivation 初期设置 Temporal 自动重试最多1次尝试」。

本文件测两件事：
1. `_bounded_retry_policy` 的纯函数契约（拒绝 0 = 无限）；
2. 观察活动**持续失败**时工作流仍能走完有界路径（不冒泡杀死工作流）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from orchestration.temporal_workflows import (
    GoalWorkflow,
    _bounded_retry_policy,
)
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"

_FIXED_ACTIVITY_ID = "act-retry-bounded-1"


# --------------------------------------------------------------- 纯函数契约


def test_rejects_unlimited_attempts() -> None:
    """`maximum_attempts=0` 在 Temporal 语义中是无限重试 —— 必须被拒绝。"""
    with pytest.raises(ValueError, match="无限重试"):
        _bounded_retry_policy(maximum_attempts=0)


# --------------------------------------------------------------- 行为：不挂死

_observe_attempts: list[int] = []


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-retry-bounded",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _stub_list(goal_id: str, owner_epoch: str) -> dict[str, list[dict[str, str]]]:
    return {"actions": [{"activity_id": _FIXED_ACTIVITY_ID, "kind": "PLAN"}]}


@activity.defn(name="admit_plan_action")
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    return {"activity_id": activity_id, "attempt_id": "attempt-1", "fencing_epoch": "1"}


@activity.defn(name="RunActivation")
async def _stub_run(payload: dict) -> dict:
    return {"status": "SUCCEEDED", "kind": payload["kind"]}


@activity.defn(name="observe_activity_status")
async def _stub_observe_always_fails(activity_id: str) -> dict[str, str]:
    """观察活动**持续失败** —— 模拟 Kernel 查询长期不可用。"""
    _observe_attempts.append(1)
    raise ApplicationError("kernel observe unavailable", non_retryable=True)


def test_persistently_failing_observe_does_not_hang_or_kill_workflow() -> None:
    """观察持续失败时：工作流仍走完有界路径并记 OBSERVE_TIMEOUT，而非挂死/崩掉。

    这是本批修复的核心行为不变量。若异常冒泡（旧行为），
    `execute_workflow` 会抛 `WorkflowFailureError` —— 既不到 OBSERVE_TIMEOUT，
    也不推进 recovery_attempts，Goal 永久卡住。
    """

    async def _run() -> dict:
        _observe_attempts.clear()
        goal_id = str(uuid4())
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_list,
                        _stub_admit,
                        _stub_observe_always_fails,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                return await env.client.execute_workflow(
                    GoalWorkflow.run,
                    {
                        "command_id": f"cmd-retry-{uuid4()}",
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "observe_max_ticks": 2,
                        "enable_continue_as_new": False,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

    try:
        result = asyncio.run(_run())
    except WorkflowFailureError as exc:  # pragma: no cover - 旧行为才会走到
        pytest.fail(f"观察失败冒泡杀死了工作流（旧缺陷行为）: {exc}")

    # 有界：观察失败不使工作流失败，而记为 OBSERVE_TIMEOUT
    assert result["marks_goal_done"] is False
    statuses = [row["status"] for row in result["plan_terminal_statuses"]]
    assert statuses == ["OBSERVE_TIMEOUT"], statuses
    # 轮次有界：失败观察被限制在 observe_max_ticks 内，未无限重试
    assert len(_observe_attempts) == 2, _observe_attempts
