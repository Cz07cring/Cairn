"""Public ArtifactResource, excluding private storage addresses."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .projects import Contract
from .verification import Digest


class ArtifactResource(Contract):
    id: UUID
    project_id: UUID
    digest: Digest
    size_bytes: int = Field(ge=0, le=9007199254740991)
    mime: str = Field(pattern=r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$", max_length=200)
    representation: Literal["RAW", "REDACTED", "TRUNCATED"]
    derived_from_artifact_id: UUID | None
    producer_identity: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    created_at: datetime
    updated_at: datetime
