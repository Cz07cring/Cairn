"""Immutable policy configuration from spec 05; creation never authorizes a tool call."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .projects import Contract

Text = Annotated[str, Field(max_length=10000)]
StringSet = Annotated[list[Text], Field(max_length=10000)]


class ExternalAction(Contract):
    action: Text
    mode: Literal["DENY", "ALLOW", "APPROVAL"]


class PolicyCreate(Contract):
    project_id: UUID
    name: Text
    allowed_tools: StringSet
    allowed_paths: StringSet
    protected_paths: StringSet
    network_allowlist: StringSet
    external_actions: list[ExternalAction] = Field(max_length=10000)
    secret_scope_refs: StringSet

    @model_validator(mode="after")
    def reject_duplicate_sets(self):
        for name in (
            "allowed_tools",
            "allowed_paths",
            "protected_paths",
            "network_allowlist",
            "secret_scope_refs",
        ):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError("duplicate set member")
        if len({a.action for a in self.external_actions}) != len(self.external_actions):
            raise ValueError("duplicate action")
        return self


class PolicyResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config: PolicyCreate
