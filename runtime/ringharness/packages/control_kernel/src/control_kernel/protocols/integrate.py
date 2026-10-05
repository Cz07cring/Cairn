"""INTEGRATE outcome：串行集成候选后再开最终屏障。"""

from uuid import UUID

from pydantic import Field

from .projects import Contract


class IntegrateSuccessOutcome(Contract):
    candidate_manifest_id: UUID
    integration_commit: str | None = Field(default=None, max_length=10000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10000)
