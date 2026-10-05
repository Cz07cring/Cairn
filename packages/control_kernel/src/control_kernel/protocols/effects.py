"""Step / Effect 协议（05§）；PLAN 禁止登记。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .goals import Digest, PositiveInt, Text
from .projects import Contract
from .runtime import LeaseIdentity

ReplayClass = Literal["READ_ONLY", "IDEMPOTENT", "RECONCILABLE", "NON_REPLAYABLE"]
EffectScope = Literal["ENGINEERING", "VERIFICATION", "RECONCILIATION", "READ_ONLY"]
EffectStatus = Literal[
    "PREPARED",
    "AUTHORIZED",
    "DISPATCHED",
    "SUCCEEDED",
    "FAILED",
    "UNKNOWN",
    "CANCELLED",
]


class StepCreate(Contract):
    lease: LeaseIdentity
    predecessor_step_id: UUID | None = None
    purpose: Text = Field(min_length=1, max_length=2000)
    tool_ref: Text = Field(min_length=1, max_length=200)


class StepResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    activity_id: UUID
    logical_step_id: UUID
    predecessor_step_id: UUID | None
    purpose: Text
    tool_ref: Text
    intent_revision: PositiveInt
    effect_id: UUID | None


class EffectPrepareRequest(Contract):
    lease: LeaseIdentity
    logical_step_id: UUID
    intent_revision: PositiveInt
    tool_ref: Text = Field(min_length=1, max_length=200)
    input_artifact_id: UUID


class EffectDispatchRequest(Contract):
    lease: LeaseIdentity
    effect_state_revision: PositiveInt


class TrustedReceipt(Contract):
    receipt_id: UUID
    effect_id: UUID
    producer_activity_id: UUID
    producer_attempt_id: UUID
    fencing_epoch: str
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    signal: str | None = None
    timed_out: bool = False
    stdout_artifact_id: UUID | None = None
    stderr_artifact_id: UUID | None = None
    result_artifact_ids: list[UUID] = Field(default_factory=list)
    external_ref: str | None = None
    observed_outcome: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]


class ReceiptAccepted(Contract):
    receipt_id: UUID
    disposition: Literal["APPLIED", "PENDING_RECONCILIATION", "DUPLICATE"]


class EffectResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    goal_id: UUID | None
    activity_id: UUID
    logical_step_id: UUID
    intent_revision: PositiveInt
    payload_digest: Digest
    tool_ref: Text
    replay_class: ReplayClass
    scope: EffectScope
    write_epoch: str | None
    status: EffectStatus
    state_revision: PositiveInt
    approval_id: UUID | None
    reservation_id: UUID | None
    external_ref: str | None
    evidence_ids: list[UUID]
    input_artifact_id: UUID


class BrokerDispatchableEffect(Contract):
    """Broker 可拉取的已准备 effect；lease 为当前 ACTIVE attempt 身份。

    非第二调度器：仅列出本 worker 租约下已 PREPARED/AUTHORIZED 的副作用意图。
    project_id 取自 effect；lease_expires_at 单独给出供宿主对账。
    """

    effect: EffectResource
    lease: LeaseIdentity
    lease_expires_at: datetime


class ReconciliationRequest(Contract):
    expected_state_revision: int = Field(ge=1)
    observed_result: Literal["SUCCEEDED", "FAILED"]
    external_ref: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=10000)
    reason: str = Field(min_length=1, max_length=2000)


class ReconcileSuccessOutcome(Contract):
    effect_id: UUID
    observed_status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10000)
