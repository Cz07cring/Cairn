"""MemoryCreate / MemoryResource：结构化记忆，模型写入始终为 PROPOSED。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_serializer

from .goals import Text
from .projects import Contract

MemoryKind = Literal["fact", "decision", "failure", "hypothesis", "question"]
MemoryStatus = Literal["PROPOSED", "VERIFIED", "SUPERSEDED"]
# 不可叠在 NonnegativeInt 上再 Field(le=…)：Pydantic 会吞掉上限约束
ConfidenceBp = Annotated[int, Field(strict=True, ge=0, le=10000)]


class MemoryCreate(Contract):
    project_id: UUID
    activity_id: UUID
    kind: MemoryKind
    statement: Text = Field(min_length=1)
    source_evidence_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    confidence_bp: ConfidenceBp
    supersedes_id: UUID | None = None


class MemoryResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    activity_id: UUID
    kind: MemoryKind
    statement: Text
    source_evidence_ids: list[UUID]
    confidence_bp: ConfidenceBp
    status: MemoryStatus
    valid_from: datetime
    supersedes_id: UUID | None = None

    @field_serializer("valid_from")
    def serialize_valid_from(self, value: datetime) -> str:
        from datetime import UTC

        if value.tzinfo is None:
            raise ValueError("valid_from 需要时区")
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class IndexMemorySuccessOutcome(Contract):
    """INDEX_MEMORY outcome；VERIFIED 晋升由 Kernel 核对 source evidence，不看 confidence_bp。"""

    record_ids: list[UUID] = Field(min_length=1, max_length=10000)
    index_version: Text = Field(min_length=1)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10000)
