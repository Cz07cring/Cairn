"""项目信任失效：EvidenceValidityDecision / TrustPropagationJob（doc/05 §3.11）。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .projects import Contract


class TrustInvalidationCreate(Contract):
    project_id: UUID
    expected_trust_revision: str = Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=32)
    evidence_id: UUID
    reason_code: str = Field(min_length=1, max_length=200)
    proof_artifact_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class TrustPropagationJobResource(Contract):
    id: UUID
    project_id: UUID
    decision_ids: list[UUID] = Field(max_length=10000)
    status: Literal["PENDING", "RUNNING", "COMPLETE", "BLOCKED"]
    cursor: str | None = Field(default=None, max_length=2000)
    affected_skill_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    affected_goal_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    affected_release_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    deadline_at: datetime
    reason_code: str | None = Field(default=None, max_length=200)
    created_at: datetime
    updated_at: datetime
