"""EvidenceEnvelope API 资源（05§；内容体对齐 Content v3）。"""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from .goals import Digest, Text
from .projects import Contract


class EvidenceEnvelopeResource(Contract):
    id: UUID
    created_at: datetime
    schema_version: int = Field(ge=3, le=3)
    project_id: UUID
    goal_id: UUID | None
    task_id: UUID | None
    producer_activity_id: UUID
    producer_attempt_id: UUID
    effect_id: UUID
    receipt_id: UUID
    contract_digest: Digest | None
    candidate_manifest_id: UUID | None
    verification_profile_id: UUID | None
    command_argv: list[Text] = Field(max_length=10000)
    input_digest: Digest
    environment_digest: Digest
    started_at: datetime
    finished_at: datetime
    exit_code: int | None
    signal: str | None
    timed_out: bool
    artifact_ids: list[UUID] = Field(max_length=10000)
    producer_identity: Text
    content_digest: Digest
