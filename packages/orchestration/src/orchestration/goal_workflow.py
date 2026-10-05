"""确定性 GoalWorkflow M1 驱动（control-loop 形状）。

这不是 Temporal replay-safe Workflow（见 temporal_workflows.GoalWorkflow）；
仅表达 ensure → get_runtime_actions → admit 的控制环。禁止 import FastAPI/apps。
M1：禁止调用模型或工具路径；admit 后即停。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from control_kernel.protocols.runtime import ActivityLease, RuntimeActionsResult
from control_kernel.storage.orchestration import admit_runtime_attempt, get_runtime_actions
from sqlalchemy import Engine, text

from .client import TemporalClient
from .protocols import DeliveryReceipt, OrchestrationBindingContent
from .relay import ensure_workflow


@dataclass
class GoalWorkflowKernelPorts:
    """可注入 Kernel/编排端口；测试用 FakeTemporalClient 与固定 subject。"""

    engine: Engine
    temporal_client: TemporalClient
    subject: str
    project_ids: list[str]
    admit_key_prefix: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class GoalWorkflowM1TickResult:
    """单次 tick 结果；不含工具调用，不声称 Goal DONE。"""

    delivery: DeliveryReceipt
    actions: RuntimeActionsResult
    admitted: list[ActivityLease]


def run_goal_workflow_m1_tick(
    ports: GoalWorkflowKernelPorts,
    binding: OrchestrationBindingContent,
    command_id: UUID,
) -> GoalWorkflowM1TickResult:
    """执行一次 M1 控制环：ensure_workflow → get_runtime_actions → admit PLAN。

    对每个 READY 且 kind=PLAN 的动作调用 admit_runtime_attempt；不调用模型/工具。
    """
    if binding.backend != "TEMPORAL":
        raise ValueError("仅 TEMPORAL 绑定可运行 GoalWorkflow M1 tick")
    if binding.goal_id is None:
        raise ValueError("GoalWorkflow M1 tick 需要 goal_id")

    delivery = ensure_workflow(
        ports.engine, binding, command_id, ports.temporal_client
    )
    actions = get_runtime_actions(
        ports.engine,
        ports.subject,
        goal_id=binding.goal_id,
        expected_owner_epoch=binding.owner_epoch,
        project_ids=ports.project_ids,
    )

    admitted: list[ActivityLease] = []
    for action in actions.actions:
        with ports.engine.connect() as db:
            kind = db.execute(
                text("SELECT kind FROM activities WHERE id=:id"),
                {"id": action.activity_id},
            ).scalar_one_or_none()
        if kind != "PLAN":
            continue
        key = f"{ports.admit_key_prefix}:{action.activity_id}"
        lease = admit_runtime_attempt(
            ports.engine,
            ports.subject,
            key,
            action.activity_id,
        )
        admitted.append(lease)

    return GoalWorkflowM1TickResult(
        delivery=delivery,
        actions=actions,
        admitted=admitted,
    )
