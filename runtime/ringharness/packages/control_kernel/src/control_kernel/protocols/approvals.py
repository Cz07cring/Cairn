"""审批协议（05§ Approval*）。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .goals import DecimalString, Digest, PositiveInt, Text
from .projects import Contract

ApprovalStatus = Literal["PENDING", "APPROVED", "DENIED", "EXPIRED", "REVOKED"]
ApprovalSubjectType = Literal["EFFECT", "MODEL_INVOCATION"]


class ApprovalSubject(Contract):
    type: ApprovalSubjectType
    id: UUID


class ApprovalScope(Contract):
    tools: list[Text] = Field(default_factory=list, max_length=10000)
    target_refs: list[Text] = Field(default_factory=list, max_length=10000)
    data_categories: list[Text] = Field(default_factory=list, max_length=10000)
    provider_refs: list[Text] = Field(default_factory=list, max_length=10000)


class ApprovalResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    goal_id: UUID | None
    state_revision: PositiveInt
    subject: ApprovalSubject
    payload_digest: Digest
    policy_version: PositiveInt
    scope: ApprovalScope
    max_cost_usd: DecimalString
    expires_at: datetime
    status: ApprovalStatus
    consumed_subject_id: UUID | None
    decision_reason: str | None = None


class ApprovalDecision(Contract):
    expected_state_revision: PositiveInt
    decision: Literal["APPROVE", "DENY"]
    subject: ApprovalSubject
    payload_digest: Digest
    reason: Text = Field(min_length=1, max_length=2000)
