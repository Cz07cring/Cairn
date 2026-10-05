"""Project contracts from spec 05; repository references are pre-registered aliases."""

from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

T = TypeVar("T")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_serializer(
        "created_at",
        "updated_at",
        "lease_expires_at",
        "started_at",
        "finished_at",
        "observed_at",
        "deadline_at",
        check_fields=False,
    )
    def serialize_resource_time(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("resource timestamps require timezone")
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ProjectCreate(Contract):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    repository_ref: str = Field(
        min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    )


class ProjectResource(ProjectCreate):
    id: UUID
    created_at: datetime
    updated_at: datetime
    state_revision: int = Field(ge=1)


class ErrorBody(Contract):
    code: str
    message: str
    details: dict[str, str] = Field(default_factory=dict)
    retryable: bool = False


class Meta(Contract):
    request_id: UUID
    next_cursor: str | None = None


class Envelope[T](Contract):
    data: T | None
    error: ErrorBody | None = None
    meta: Meta
