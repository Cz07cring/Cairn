"""Skill / SkillSet 合同类型（05§4）；激活与验收记录由存储层核对。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .goals import Criterion, Text
from .projects import Contract
from .verification import Digest

StringSet = Annotated[list[Text], Field(max_length=10000)]


class SkillCreate(Contract):
    project_id: UUID
    name: Text
    source_ref: Text
    content_artifact_id: UUID
    capabilities: StringSet
    role_scopes: StringSet
    required_tools: StringSet
    verification_profile_id: UUID
    acceptance: list[Criterion] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_skill(self):
        for field in ("capabilities", "role_scopes", "required_tools"):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError("duplicate set member")
        if not any(c.required for c in self.acceptance):
            raise ValueError("at least one required criterion")
        if len({c.id for c in self.acceptance}) != len(self.acceptance):
            raise ValueError("duplicate criterion id")
        if any(c.verification_profile_id != self.verification_profile_id for c in self.acceptance):
            raise ValueError("acceptance profile mismatch")
        return self


class SkillVersionResource(Contract):
    id: UUID
    skill_id: UUID
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)
    content_digest: Digest
    config: SkillCreate
    status: Literal["CANDIDATE", "VALIDATING", "ACTIVE", "REVOKED", "REJECTED"]
    audit_id: UUID | None
    revocation_reason: str | None = Field(default=None, max_length=2000)


class SkillActivate(Contract):
    audit_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class SkillRevoke(Contract):
    reason: str = Field(min_length=1, max_length=2000)


class CriterionResult(Contract):
    criterion_id: Text
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    evidence_ids: list[UUID] = Field(max_length=10000)
    reason: Text


class SkillValidationSubmit(Contract):
    """VALIDATE_SKILL outcome 业务内容；producer/id/digest 由 Kernel 写入。"""

    subject_digest: Digest
    verification_profile_id: UUID
    audit_round: int = Field(ge=1)
    verifier_run_ids: list[UUID] = Field(max_length=10000)
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    criterion_results: list[CriterionResult] = Field(min_length=1, max_length=10000)
    evidence_ids: list[UUID] = Field(max_length=10000)
    reason: Text


class ValidateSkillSuccessOutcome(Contract):
    version_id: UUID
    validation: SkillValidationSubmit


class SkillValidationRecordResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    content_digest: Digest
    project_id: UUID
    producer_activity_id: UUID
    producer_attempt_id: UUID
    subject_skill_version_id: UUID
    subject_digest: Digest
    verification_profile_id: UUID
    audit_round: int = Field(ge=1)
    verifier_run_ids: list[UUID] = Field(max_length=10000)
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    criterion_results: list[CriterionResult] = Field(max_length=10000)
    evidence_ids: list[UUID] = Field(max_length=10000)
    reason: Text


class SkillSetCreate(Contract):
    project_id: UUID
    name: Text
    skill_version_ids: list[UUID] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_versions(self):
        if len(set(self.skill_version_ids)) != len(self.skill_version_ids):
            raise ValueError("duplicate skill version")
        return self


class SkillSetResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)
    content_digest: Digest
    config: SkillSetCreate
