"""TM08 部分举证：Continue-As-New 前后命令不丢、不重消费。

测试专用 `CommandWaterlineWorkflow`：用消费水位（waterline）跨 CAN 承接；
生产 `GoalWorkflow` 的 observe-timeout CAN 水位见 `test_goal_workflow_continue_as_new.py`。
真 temporalio `start_time_skipping`；禁 LEGACY；不标 Goal DONE。

注意：返回/查询类型须用 `dict[str, Any]`，不可用 `dict[str, object]`——
temporalio JSON 转换器无法把 list 值转成 object（会炸 command_ids_consumed）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.client import WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# 进程内幂等台账：Activity 侧证明「再投不双计」
_record_calls: list[str] = []
_consumed_ledger: set[str] = set()


def _reset_ledger() -> None:
    _record_calls.clear()
    _consumed_ledger.clear()


@activity.defn(name="record_command")
async def _record_command(command_id: str) -> dict[str, Any]:
    """幂等记录命令消费（set）；重复投递返回 duplicate=True。"""
    cid = str(command_id)
    _record_calls.append(cid)
    duplicate = cid in _consumed_ledger
    _consumed_ledger.add(cid)
    return {
        "command_id": cid,
        "duplicate": duplicate,
        "unique_count": len(_consumed_ledger),
    }


@workflow.defn(name="CommandWaterlineWorkflow")
class CommandWaterlineWorkflow:
    """测试专用：Continue-As-New 携带命令消费水位（TM08 部分模式）。

    生产 GoalWorkflow CAN 须同样携带 consume waterline；本类不进入 Worker 生产注册。
    """

    def __init__(self) -> None:
        self._inbox: list[str] = []
        self._finish: bool = False
        self._consumed: list[str] = []
        self._generation: int = 0

    @workflow.signal
    async def deliver_command(self, command_id: str) -> None:
        """投递一条命令 id（可在 CAN 后重投以验证不双计）。"""
        self._inbox.append(str(command_id))

    @workflow.signal
    async def finish(self) -> None:
        """结束等待，返回水位摘要。"""
        self._finish = True

    @workflow.query
    def waterline(self) -> dict[str, Any]:
        """查询当前 run 的消费水位与身份。"""
        info = workflow.info()
        return {
            "command_ids_consumed": list(self._consumed),
            "inbox": list(self._inbox),
            "generation": self._generation,
            "workflow_id": info.workflow_id,
            "run_id": info.run_id,
        }

    @workflow.run
    async def run(self, payload: dict | None = None) -> dict[str, Any]:
        data = payload or {}
        self._consumed = [str(x) for x in (data.get("command_ids_consumed") or [])]
        consumed_set = set(self._consumed)
        self._generation = int(data.get("generation") or 0)
        continue_after = int(data.get("continue_after") or 0)
        prior_run_id = data.get("prior_run_id")

        pending = data.get("pending_command_id")
        if pending:
            self._inbox.append(str(pending))

        processed_this_run = 0
        info = workflow.info()

        while True:
            await workflow.wait_condition(
                lambda: bool(self._inbox) or self._finish
            )
            if self._finish and not self._inbox:
                break

            cmd = self._inbox.pop(0)
            await workflow.execute_activity(
                "record_command",
                args=[cmd],
                start_to_close_timeout=timedelta(seconds=30),
            )
            # 水位列表不因重复 Activity 调用而双写
            if cmd not in consumed_set:
                consumed_set.add(cmd)
                self._consumed.append(cmd)

            processed_this_run += 1

            # 达到步数或 Server 建议 CAN：携带水位 + 未决 inbox 首条
            should_can = (
                (continue_after > 0 and processed_this_run >= continue_after)
                or workflow.info().is_continue_as_new_suggested()
            )
            if should_can:
                # 未决首条经 payload 防丢（测试约定 CAN 前 inbox≤1，见 continue_after）
                next_pending = self._inbox[0] if self._inbox else None
                workflow.continue_as_new(
                    {
                        "command_ids_consumed": list(self._consumed),
                        "pending_command_id": next_pending,
                        "continue_after": 0,
                        "generation": self._generation + 1,
                        "prior_run_id": info.run_id,
                    }
                )

        return {
            "ok": True,
            "command_ids_consumed": list(self._consumed),
            "consumed_count": len(self._consumed),
            "generation": self._generation,
            "workflow_id": info.workflow_id,
            "run_id": workflow.info().run_id,
            "prior_run_id": prior_run_id,
            "marks_goal_done": False,
        }


async def _wait_generation(
    handle: WorkflowHandle,
    min_generation: int,
    *,
    attempts: int = 80,
) -> dict[str, Any]:
    """轮询 query，直到 CAN 后的新 run 暴露 generation。"""
    last: dict[str, Any] = {}
    for _ in range(attempts):
        last = await handle.query(CommandWaterlineWorkflow.waterline)
        if int(last.get("generation") or 0) >= min_generation:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"等待 generation>={min_generation} 超时；最后 waterline={last!r}"
    )


def test_tm08_continue_as_new_keeps_waterline_no_double_consume():
    """pending A → CAN 后 A 在水位；重投 A 不双计；新命令 B 消费一次；workflow_id 不变。"""

    async def _run() -> None:
        _reset_ledger()
        task_queue = f"tm08-{uuid4()}"
        workflow_id = f"tm08-waterline-{uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            # 与 sibling temporal 测一致：env 与 Worker 之间隔一行，避免 SIM117 合并
            assert env.client is not None
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[CommandWaterlineWorkflow],
                activities=[_record_command],
            ):
                handle = await env.client.start_workflow(
                    CommandWaterlineWorkflow.run,
                    {
                        "command_ids_consumed": [],
                        "pending_command_id": "cmd-A",
                        "continue_after": 1,
                        "generation": 0,
                    },
                    id=workflow_id,
                    task_queue=task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )

                # CAN 后 generation=1，且 A 已在水位
                wl = await _wait_generation(handle, 1)
                assert wl["workflow_id"] == workflow_id
                assert "cmd-A" in wl["command_ids_consumed"]
                assert list(wl["command_ids_consumed"]).count("cmd-A") == 1
                run_after_can = str(wl["run_id"])
                assert run_after_can  # 新 run_id

                # 重投 A：Activity 可再调，水位不双写
                await handle.signal(CommandWaterlineWorkflow.deliver_command, "cmd-A")
                # 新命令 B：应消费一次
                await handle.signal(CommandWaterlineWorkflow.deliver_command, "cmd-B")
                await handle.signal(CommandWaterlineWorkflow.finish)

                result = await handle.result()
                assert result["ok"] is True
                assert result["marks_goal_done"] is False
                assert result["workflow_id"] == workflow_id
                # 身份映射：同一 workflow_id，run_id 相对首跑已更新
                assert result["prior_run_id"]  # CAN 携带上一 run
                assert result["run_id"] != result["prior_run_id"]
                assert result["command_ids_consumed"] == ["cmd-A", "cmd-B"]
                assert result["consumed_count"] == 2

                # Activity 台账：A 至少两次调用（首消费 + 重投），唯一集仍为 {A,B}
                assert _record_calls.count("cmd-A") >= 2
                assert _record_calls.count("cmd-B") == 1
                assert _consumed_ledger == {"cmd-A", "cmd-B"}

    asyncio.run(_run())


def test_tm08_pending_survives_can_when_carried_in_payload():
    """模拟 CAN 入参：已消费 A + pending B → 新 run 不丢 B，且 A 仍在水位。"""

    async def _run() -> None:
        _reset_ledger()
        # 模拟上一 run 已幂等落账的 A（生产侧应对齐 PG / 水位）
        _consumed_ledger.add("cmd-A")
        task_queue = f"tm08-pend-{uuid4()}"
        workflow_id = f"tm08-pending-{uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[CommandWaterlineWorkflow],
                activities=[_record_command],
            ):
                # 直接以「CAN 后」payload 启动：水位已有 A，pending 携带 B
                handle = await env.client.start_workflow(
                    CommandWaterlineWorkflow.run,
                    {
                        "command_ids_consumed": ["cmd-A"],
                        "pending_command_id": "cmd-B",
                        "continue_after": 0,
                        "generation": 1,
                        "prior_run_id": "run-before-can",
                    },
                    id=workflow_id,
                    task_queue=task_queue,
                )
                # 等 B 进入水位后再 finish（避免固定 sleep 竞态）
                for _ in range(80):
                    wl = await handle.query(CommandWaterlineWorkflow.waterline)
                    if "cmd-B" in (wl.get("command_ids_consumed") or []):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("pending cmd-B 未在 CAN 后 run 消费")

                await handle.signal(CommandWaterlineWorkflow.finish)
                result = await handle.result()
                assert result["command_ids_consumed"] == ["cmd-A", "cmd-B"]
                assert result["prior_run_id"] == "run-before-can"
                assert result["workflow_id"] == workflow_id
                assert _consumed_ledger == {"cmd-A", "cmd-B"}
                # A 仅经水位继承，本 run 不应再 record A
                assert "cmd-A" not in _record_calls
                assert _record_calls.count("cmd-B") == 1

    asyncio.run(_run())
