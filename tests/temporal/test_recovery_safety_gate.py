"""门3 恢复安全门：CAN/恢复不得执行过期或版本不兼容意图。

过期 / schema 不兼容 / 次数超限 → RECOVERY_ABANDONED；
不新增 admit/RunActivation；marks_goal_done 恒 False；
DISPATCHED/UNKNOWN 不在本测范围（防线仍在 effect 层）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"
_FIXED_ACTIVITY_ID = "act-recovery-gate-1"

_admit_calls: list[str] = []
_run_activation_calls: list[str] = []


def _reset() -> None:
    _admit_calls.clear()
    _run_activation_calls.clear()
    _abandon_calls.clear()


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-recovery-stub",
        "delivery_status": "ACKNOWLEDGED",
    }


_abandon_calls: list[dict[str, object]] = []


@activity.defn(name="record_recovery_abandonment")
async def _stub_record_abandonment(
    goal_id: str, reason: str, generation: int, prior_run_id: str | None
) -> dict[str, object]:
    """Issue #24 接线桩：记录放弃裁决落库调用（真实 Kernel 侧写 PG，此处只记调用）。"""
    _abandon_calls.append(
        {
            "goal_id": goal_id,
            "reason": reason,
            "generation": generation,
            "prior_run_id": prior_run_id,
        }
    )
    return {
        "abandonment_id": "abandon-test-1",
        "goal_id": goal_id,
        "generation": generation,
        "reason": reason,
        "prior_run_id": prior_run_id,
        "marks_goal_done": False,
    }


@activity.defn(name="list_runtime_actions")
async def _stub_list(goal_id: str, owner_epoch: str) -> dict:
    return {
        "actions": [
            {
                "activity_id": _FIXED_ACTIVITY_ID,
                "action_id": _FIXED_ACTIVITY_ID,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="admit_plan_action")
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    _admit_calls.append(activity_id)
    return {
        "activity_id": activity_id,
        "attempt_id": "attempt-recovery-1",
        "fencing_epoch": "1",
    }


@activity.defn(name="RunActivation")
async def _stub_run(payload: dict) -> dict:
    _run_activation_calls.append(str(payload.get("activity_id") or ""))
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "should-not-run-on-abandoned-recovery",
        "kind": payload["kind"],
        "pending_harness": True,
    }


@activity.defn(name="observe_activity_status")
async def _stub_observe(activity_id: str) -> dict[str, str]:
    return {"activity_id": activity_id, "status": "RUNNING", "kind": "PLAN"}


@activity.defn(name="observe_carried_plan_activity_status")
async def _stub_observe_carried(
    goal_id: str, owner_epoch: str, activity_id: str
) -> dict[str, str]:
    return await _stub_observe(activity_id)


@activity.defn(name="observe_carried_activation_activity_status")
async def _stub_observe_carried_activation(
    goal_id: str, owner_epoch: str, activity_id: str
) -> dict[str, str]:
    return await _stub_observe(activity_id)


def _abandoned_payload(**extra: object) -> dict:
    base = {
        "command_id": f"cmd-recovery-{uuid4()}",
        "goal_id": str(uuid4()),
        "owner_epoch": "1",
        "generation": 1,
        "recovery_attempts": 1,
        "checkpoint_schema_version": 1,
        "enable_continue_as_new": True,
        # 与 CAN 分离的恢复总开关；本夹具测其它放弃原因时须显式打开
        "recovery_enabled": True,
        "skip_admit_activity_ids": [_FIXED_ACTIVITY_ID],
        "observe_max_ticks": 1,
    }
    base.update(extra)
    return base


def test_recovery_abandons_when_intent_expired():
    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            # 过期意图：valid_until 在「工作流时间」之前
            past = "2000-01-01T00:00:00+00:00"
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                goal_id = str(uuid4())
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    _abandoned_payload(
                        goal_id=goal_id,
                        command_id=f"cmd-{goal_id}",
                        intent_valid_until=past,
                    ),
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        assert result["ok"] is False
        assert result["recovery_status"] == "RECOVERY_ABANDONED"
        assert result["recovery_reason"] == "INTENT_EXPIRED"
        assert result["marks_goal_done"] is False
        assert _admit_calls == []
        assert _run_activation_calls == []

    asyncio.run(_run())


def test_recovery_abandons_when_schema_incompatible():
    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            future = "2099-01-01T00:00:00+00:00"
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                goal_id = str(uuid4())
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    _abandoned_payload(
                        goal_id=goal_id,
                        command_id=f"cmd-{goal_id}",
                        intent_valid_until=future,
                        checkpoint_schema_version=99,
                    ),
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        assert result["recovery_status"] == "RECOVERY_ABANDONED"
        assert result["recovery_reason"] == "CHECKPOINT_SCHEMA_INCOMPATIBLE"
        assert result["marks_goal_done"] is False
        assert _run_activation_calls == []

    asyncio.run(_run())


def test_recovery_abandons_when_attempts_exceeded():
    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            future = "2099-01-01T00:00:00+00:00"
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                goal_id = str(uuid4())
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    _abandoned_payload(
                        goal_id=goal_id,
                        command_id=f"cmd-{goal_id}",
                        intent_valid_until=future,
                        recovery_attempts=4,
                        max_recovery_attempts=3,
                    ),
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        assert result["recovery_status"] == "RECOVERY_ABANDONED"
        assert result["recovery_reason"] == "RECOVERY_ATTEMPTS_EXCEEDED"
        assert result["marks_goal_done"] is False
        assert _run_activation_calls == []

    asyncio.run(_run())


def test_recovery_abandons_when_recovery_disabled():
    """generation>0 且未显式 recovery_enabled → RECOVERY_DISABLED；0 admit / 0 RunActivation。"""

    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            future = "2099-01-01T00:00:00+00:00"
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                goal_id = str(uuid4())
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    _abandoned_payload(
                        goal_id=goal_id,
                        command_id=f"cmd-{goal_id}",
                        intent_valid_until=future,
                        recovery_enabled=False,
                    ),
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        assert result["ok"] is False
        assert result["recovery_status"] == "RECOVERY_ABANDONED"
        assert result["recovery_reason"] == "RECOVERY_DISABLED"
        assert result["marks_goal_done"] is False
        assert _admit_calls == []
        assert _run_activation_calls == []

    asyncio.run(_run())


def test_recovery_abandons_when_recovery_enabled_omitted():
    """缺省 recovery_enabled 视为关闭（失败关闭）。"""

    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            future = "2099-01-01T00:00:00+00:00"
            payload = _abandoned_payload(
                intent_valid_until=future,
            )
            del payload["recovery_enabled"]
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                goal_id = str(uuid4())
                payload["goal_id"] = goal_id
                payload["command_id"] = f"cmd-{goal_id}"
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    payload,
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        assert result["recovery_reason"] == "RECOVERY_DISABLED"
        assert result["marks_goal_done"] is False
        assert _admit_calls == []

    asyncio.run(_run())


def test_recovery_allows_when_attempts_equal_max():
    """recovery_attempts == max 仍可通过闸（仅严格大于才放弃）。"""

    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            future = "2099-01-01T00:00:00+00:00"
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                goal_id = str(uuid4())
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    _abandoned_payload(
                        goal_id=goal_id,
                        command_id=f"cmd-{goal_id}",
                        intent_valid_until=future,
                        recovery_attempts=3,
                        max_recovery_attempts=3,
                        recovery_enabled=True,
                        # 关闭 CAN，避免观察超时续跑把 attempts+1 误判为超限
                        enable_continue_as_new=False,
                    ),
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        assert result.get("recovery_status") != "RECOVERY_ABANDONED"
        assert result["marks_goal_done"] is False
        # skip_admit：不新 admit，但过闸后会观察
        assert _admit_calls == []

    asyncio.run(_run())


def test_recovery_abandons_naive_intent_deadline():
    """无时区 deadline 解析失败 → schema 不兼容放弃。"""

    async def _run() -> None:
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                goal_id = str(uuid4())
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    _abandoned_payload(
                        goal_id=goal_id,
                        command_id=f"cmd-{goal_id}",
                        intent_valid_until="2099-01-01T00:00:00",
                        recovery_enabled=True,
                    ),
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        assert result["recovery_reason"] == "CHECKPOINT_SCHEMA_INCOMPATIBLE"
        assert _admit_calls == []

    asyncio.run(_run())


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure_budget_exhausted(
    command_id: str, goal_id: str
) -> dict[str, object]:
    """投递成功但 wall-clock 预算已耗尽。"""
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-budget-stub",
        "delivery_status": "ACKNOWLEDGED",
        "budget_remaining_wall_seconds": 0,
        "budget_exhausted": True,
    }


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure_legacy(command_id: str, goal_id: str) -> dict[str, object]:
    """模拟**改动前**记录的历史结果：无任何预算字段。"""
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-legacy-stub",
        "delivery_status": "ACKNOWLEDGED",
    }


async def _run_with_ensure(ensure_activity, payload: dict) -> dict:
    """以指定 delivery stub 跑一次 GoalWorkflow，返回结果。"""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        assert env.client is not None
        async with (
            Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[
                    ensure_activity,
                    _stub_record_abandonment,
                    _stub_list,
                    _stub_admit,
                    _stub_observe,
                    _stub_observe_carried,
                    _stub_observe_carried_activation,
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
                payload,
                id=f"goal-{payload['goal_id']}",
                task_queue=_CONTROL_QUEUE,
                execution_timeout=timedelta(seconds=30),
            )


def test_wall_clock_budget_exhausted_fails_closed_with_zero_admit() -> None:
    """Codex P1-3：预算耗尽必须失败关闭——0 admit / 0 RunActivation，不靠 1 秒窗口碰运气。"""

    async def _run() -> None:
        _reset()
        payload = _abandoned_payload(command_id="cmd-budget-exhausted")
        result = await _run_with_ensure(_stub_ensure_budget_exhausted, payload)

        assert result["ok"] is False
        assert result["recovery_status"] == "RECOVERY_ABANDONED"
        assert result["recovery_reason"] == "WALL_CLOCK_BUDGET_EXHAUSTED"
        assert result["marks_goal_done"] is False
        assert _admit_calls == []
        assert _run_activation_calls == []

    asyncio.run(_run())


def test_legacy_delivery_without_budget_fields_is_not_budget_abandoned() -> None:
    """重放安全依据：历史 delivery 无预算字段时不得触发预算放弃（逐字保留旧行为）。

    旧历史缺 `budget_remaining_wall_seconds`/`budget_exhausted`，两条取值分支都必须
    与改动前一致，否则已记录的历史重放会得到不同命令序列。
    """

    async def _run() -> None:
        _reset()
        # 意图未过期 + schema 兼容 + 次数未超限 → 旧行为下会继续推进（不被放弃）
        future = "2999-01-01T00:00:00+00:00"
        payload = _abandoned_payload(
            command_id="cmd-legacy", intent_valid_until=future
        )
        result = await _run_with_ensure(_stub_ensure_legacy, payload)

        # 关键：不得因**预算**而放弃。旧行为里没有预算分支，故原因必须是既有的那条。
        # 本 stub 场景下 observe 恒超时 → 反复 CAN → 最终走既有的 attempts 超限放弃；
        # 该路径由第一百零二批引入，与本批无关。钉住它可防止"预算改动悄悄改写了老路径"。
        assert result.get("recovery_reason") == "RECOVERY_ATTEMPTS_EXCEEDED", result
        assert result["marks_goal_done"] is False
        assert result["admitted_activity_ids"] == []

    asyncio.run(_run())

_ensure_budget_calls: list[int] = []


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure_budget_progressing(
    command_id: str, goal_id: str
) -> dict[str, object]:
    """模拟**时间推进**：首次投递预算仍充足，CAN 之后的下一代投递时已耗尽。

    这不是"一开始就耗尽"的静态场景 —— 它对应 Codex 解除条件第 4 条：
    `elapsed` 从 0 推进到 limit 后，**下一代 CAN 必须在危险动作之前退出**。
    """
    _ensure_budget_calls.append(1)
    exhausted = len(_ensure_budget_calls) > 1
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-budget-progress",
        "delivery_status": "ACKNOWLEDGED",
        "budget_status": "OK",
        "budget_remaining_wall_seconds": 0 if exhausted else 3600,
        "budget_exhausted": exhausted,
    }


def test_budget_progressing_to_limit_exits_before_next_generation_danger() -> None:
    """Codex 解除条件第 4 条：预算随时间推进到耗尽后，下一代 CAN 必须先行放弃。

    流程：
    - 第 1 代（generation=0）预算充足 → 正常 admit + RunActivation；observe 不达终态
      → OBSERVE_TIMEOUT → Continue-As-New 到第 2 代；
    - 第 2 代投递时读数显示已耗尽 → **在任何 admit/RunActivation 之前**返回
      WALL_CLOCK_BUDGET_EXHAUSTED，且标记不写 Goal DONE。

    断言侧重"**新增**的危险动作数为 0"：admit/RunActivation 各只应出现第 1 代那一次。
    """

    async def _run() -> None:
        _reset()
        _ensure_budget_calls.clear()
        goal_id = str(uuid4())
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure_budget_progressing,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    {
                        "command_id": f"cmd-budget-progress-{uuid4()}",
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "generation": 0,
                        "observe_max_ticks": 1,
                        "enable_continue_as_new": True,
                        "continue_as_new_on_observe_timeout": True,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

        # 第 1 代确实推进过（否则本测没有意义）
        assert len(_ensure_budget_calls) >= 2, "应发生 Continue-As-New 进入下一代"
        # 第 2 代：耗尽 → 放弃
        assert result["ok"] is False
        assert result["recovery_status"] == "RECOVERY_ABANDONED"
        assert result["recovery_reason"] == "WALL_CLOCK_BUDGET_EXHAUSTED"
        assert result["marks_goal_done"] is False
        # **关键**：第 2 代没有新增任何危险动作
        assert _admit_calls == [_FIXED_ACTIVITY_ID]
        assert _run_activation_calls == [_FIXED_ACTIVITY_ID]

    asyncio.run(_run())

def test_abandonment_is_persisted_before_workflow_returns():
    """Issue #24：放弃裁决必须先落库再结束工作流（否则 Goal 静默永久卡住）。

    可判别性：断言 `record_recovery_abandonment` **确实被调用**且 reason 正确。
    若接线被移除，本测失败 —— 这正是本 Issue 要防的回归。
    """

    async def _run() -> dict:
        _reset()
        env = await WorkflowEnvironment.start_time_skipping()
        async with env:
            assert env.client is not None
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_record_abandonment,
                        _stub_list,
                        _stub_admit,
                        _stub_observe,
                        _stub_observe_carried,
                        _stub_observe_carried_activation,
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
                    _abandoned_payload(),
                    id=f"goal-{uuid4()}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

    result = asyncio.run(_run())

    assert result["recovery_status"] == "RECOVERY_ABANDONED"
    assert result["marks_goal_done"] is False
    # **核心**：落库调用确实发生，且 reason 与工作流裁决一致
    assert len(_abandon_calls) == 1, _abandon_calls
    assert _abandon_calls[0]["reason"] == result["recovery_reason"]
    assert _abandon_calls[0]["goal_id"]
    # 幂等前提：同 generation 重复调用由 Kernel 侧 (goal_id, generation) 去重
    assert int(_abandon_calls[0]["generation"]) >= 0

