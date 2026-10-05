"""编排 Relay：消费 PENDING 投递并 ACK；依赖 Kernel storage + 可注入客户端。"""

from __future__ import annotations

from uuid import UUID

from control_kernel.storage.orchestration import (
    EVENT_ENSURE_WORKFLOW,
    acknowledge_delivery,
    get_binding_for_goal,
    get_delivery_for_command,
)
from control_kernel.storage.policies import ScopeNotFound
from sqlalchemy import Engine

from .client import TemporalClient
from .protocols import DeliveryReceipt, OrchestrationBindingContent


def ensure_workflow(
    engine: Engine,
    binding: OrchestrationBindingContent,
    command_id: UUID,
    client: TemporalClient,
    **start_kwargs: object,
) -> DeliveryReceipt:
    """确保 Workflow 已启动并 ACK 投递；幂等返回同一 DeliveryReceipt。

    TemporalUnavailable 时保持 PENDING，不回退 LEGACY、不翻转 Goal backend。
    ``start_kwargs`` 透传给 ``client.ensure_started``（如 observe_max_ticks）。
    """
    if binding.backend != "TEMPORAL":
        raise ValueError("仅 TEMPORAL 绑定可 ensure_workflow")
    if binding.goal_id is None:
        raise ValueError("ensure_workflow 需要 goal_id")

    with engine.begin() as db:
        stored = get_binding_for_goal(db, binding.goal_id)
        if stored is None:
            raise ScopeNotFound()
        if (
            stored["project_id"] != binding.project_id
            or stored["owner_epoch"] != binding.owner_epoch
            or stored["workflow_id"] != binding.workflow_id
        ):
            raise ValueError("绑定与持久记录的项目/owner/workflow 不一致")

        delivery = get_delivery_for_command(db, command_id, EVENT_ENSURE_WORKFLOW)
        if delivery is None:
            raise ScopeNotFound()
        if delivery["delivery_status"] == "ACKNOWLEDGED":
            return DeliveryReceipt(
                command_id=command_id,
                workflow_id=delivery["workflow_id"],
                run_id=delivery["run_id"],
                delivery_status="ACKNOWLEDGED",
            )

    # 客户端调用在事务外：失败时不得误 ACK
    run_id = client.ensure_started(
        binding.workflow_id,
        namespace=binding.namespace,
        worker_build_id=binding.worker_build_id,
        command_id=str(command_id),
        goal_id=str(binding.goal_id),
        owner_epoch=str(binding.owner_epoch),
        **start_kwargs,
    )

    with engine.begin() as db:
        updated = acknowledge_delivery(db, command_id=command_id, run_id=run_id)
        return DeliveryReceipt(
            command_id=command_id,
            workflow_id=updated["workflow_id"],
            run_id=updated["run_id"],
            delivery_status=updated["delivery_status"],
        )
