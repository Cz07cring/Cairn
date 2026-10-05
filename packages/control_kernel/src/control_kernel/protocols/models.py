"""Model configuration describes intent; capability requires a separate trusted probe."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .goals import DecimalString, Digest, NonnegativeInt, PositiveInt, Text
from .policies import StringSet
from .projects import Contract
from .runtime import LeaseIdentity


class ModelProfileCreate(Contract):
    project_id: UUID
    name: Text
    local_provider_ref: Text
    model_id: Text
    context_window: PositiveInt
    output_reserve: NonnegativeInt
    inference_slots: PositiveInt
    cloud_provider_refs: StringSet
    cloud_mode: Literal["DENY", "PREAUTHORIZED", "APPROVAL"]

    @model_validator(mode="after")
    def check_limits(self):
        if self.output_reserve >= self.context_window:
            raise ValueError("output reserve must leave input capacity")
        if len(set(self.cloud_provider_refs)) != len(self.cloud_provider_refs):
            raise ValueError("duplicate provider")
        return self


class ModelProfileResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    version: PositiveInt
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config: ModelProfileCreate
    capability_status: Literal["UNVERIFIED", "VERIFIED", "FAILED"] = "UNVERIFIED"
    probe_evidence_ids: list[UUID] = Field(default_factory=list)


class ProbeSuccessOutcome(Contract):
    profile_id: UUID
    capability_status: Literal["VERIFIED", "FAILED"]
    evidence_ids: list[UUID] = Field(default_factory=list)


class ContextBindRequest(Contract):
    lease: LeaseIdentity
    binding_digest: Digest
    context_bundle_id: UUID


class ContextBindResponse(Contract):
    context_digest: Digest
    binding_digest: Digest


class ModelInvocationCreate(Contract):
    lease: LeaseIdentity
    invocation_seq: PositiveInt
    context_digest: Digest
    input_digest: Digest
    provider_ref: Text
    model_id: Text
    max_output_tokens: PositiveInt
    max_cost_usd: DecimalString
    data_categories: list[Text] = Field(default_factory=list, max_length=10000)
    # 运行时登记：暴露给模型的工具名；不进 Content v3 ModelInvocationInput
    exposed_tools: list[Text] = Field(default_factory=list, max_length=100)


class ModelToolCall(Contract):
    """live dispatch 回传的单次 tool-call；不持久化进 Content 摘要。"""

    id: Text
    name: Text
    arguments: Text


class ModelInvocationResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    goal_id: UUID | None
    activity_id: UUID
    producer_attempt_id: UUID
    invocation_seq: PositiveInt
    payload_digest: Digest
    binding_digest: Digest
    context_digest: Digest
    input_digest: Digest
    provider_ref: Text
    model_id: Text
    max_output_tokens: PositiveInt
    max_cost_usd: DecimalString
    data_categories: list[Text]
    exposed_tools: list[Text] = Field(default_factory=list)
    status: Literal[
        "PREPARED",
        "AWAITING_APPROVAL",
        "AUTHORIZED",
        "DISPATCHED",
        "SUCCEEDED",
        "FAILED",
        "UNKNOWN",
        "CANCELLED",
    ]
    state_revision: PositiveInt
    approval_id: UUID | None = None
    reservation_id: UUID
    usage_status: Literal["CONFIRMED", "UNKNOWN"]
    result_artifact_id: UUID | None = None
    # 仅 live dispatch 响应可能带；不进持久化摘要，供 Runner 取模型正文 / tool-call
    assistant_text: Text | None = None
    tool_calls: list[ModelToolCall] | None = None


class ModelDispatchRequest(Contract):
    lease: LeaseIdentity
    expected_state_revision: PositiveInt
    # Runner 已/将自行调用模型（官方 AgentLoop 多轮 tools）；Control 只迁 DISPATCHED，
    # 不代调 chat。随后须由持有者 POST receipts；≠ Goal DONE。
    runner_owned_completion: bool = False


class ModelReceipt(Contract):
    receipt_id: UUID
    invocation_id: UUID
    producer_attempt_id: UUID
    observed_result: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    usage_status: Literal["CONFIRMED", "UNKNOWN"]
    input_tokens: NonnegativeInt | None = None
    output_tokens: NonnegativeInt | None = None
    cost_usd: DecimalString | None = None
    # 连接器可得时上报；null/缺省表示本回执无 GPU 用量证据
    gpu_seconds: DecimalString | None = None
    result_artifact_id: UUID | None = None
    observed_at: datetime


class ModelReceiptResponse(Contract):
    disposition: Literal["APPLIED", "PENDING_RECONCILIATION", "DUPLICATE"]
