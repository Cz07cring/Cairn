"""Validated workload/user claims from the fixed external issuer."""

from pydantic import BaseModel, ConfigDict, Field


class Principal(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    sub: str = Field(min_length=1, max_length=200)
    roles: list[str]
    project_ids: list[str]
