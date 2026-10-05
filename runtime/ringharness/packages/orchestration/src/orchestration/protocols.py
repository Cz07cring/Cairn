"""编排内部契约（doc/v0.6/05 §2）；纯 pydantic，无网络/文件/DB IO。"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from control_kernel.protocols.runtime import (
    RuntimeActionRef,
    RuntimeActionsResult,
    RuntimeAdmitRequest,
    RuntimeWaitHint,
)
from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Epoch = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=32)]
OrchestrationBackend = Literal["LEGACY", "TEMPORAL"]
DeliveryStatus = Literal["PENDING", "ACKNOWLEDGED"]

__all__ = [
    "DeliveryReceipt",
    "DeliveryStatus",
    "Digest",
    "Epoch",
    "OrchestrationBackend",
    "OrchestrationBindingContent",
    "RuntimeActionRef",
    "RuntimeActionsResult",
    "RuntimeAdmitRequest",
    "RuntimeWaitHint",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrchestrationBindingContent(Contract):
    """编排绑定内容；Workflow ID 为稳定业务派生标识。"""

    project_id: UUID
    goal_id: UUID | None
    budget_scope_id: UUID
    backend: OrchestrationBackend
    owner_epoch: Epoch
    namespace: str = Field(min_length=1, max_length=200)
    workflow_id: str = Field(min_length=1, max_length=500)
    active_run_id: str | None = Field(default=None, max_length=500)
    worker_build_id: str = Field(min_length=1, max_length=200)
    contract_digest: Digest


class DeliveryReceipt(Contract):
    """投递回执；ACKNOWLEDGED 只表示编排侧收到，不表示 Goal/Activity DONE。"""

    command_id: UUID
    workflow_id: str = Field(min_length=1, max_length=500)
    run_id: str | None = Field(default=None, max_length=500)
    delivery_status: DeliveryStatus
