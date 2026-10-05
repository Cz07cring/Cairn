"""Verifier/profile contracts; trust and referenced bytes are validated by the registry."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .goals import PositiveInt, Text
from .projects import Contract
from .runtime import LeaseIdentity

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Layer = Literal["MECHANICAL", "SEMANTIC", "ADVERSARIAL", "GLOBAL"]
ValueType = Literal["INTEGER", "DECIMAL", "BOOLEAN", "STRING"]
Epoch = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=32)]


class MetricDefinition(Contract):
    metric: Text
    value_type: ValueType
    unit: Text


class VerifierDefinition(Contract):
    project_id: UUID
    name: str = Field(min_length=1, max_length=10000)
    layer: Layer
    image_digest: Digest
    entrypoint_ref: str = Field(min_length=1, max_length=10000)
    input_schema_digest: Digest
    output_schema_digest: Digest
    timeout_seconds: PositiveInt
    metric_definitions: list[MetricDefinition] = Field(min_length=1, max_length=10000)
    rubric_artifact_id: UUID | None
    fixture_set_digest: Digest
    uncertain_rule: Literal["INSUFFICIENT"]
    timeout_rule: Literal["INFRA_UNLESS_TRUSTED_METRIC"]
    network_policy_digest: Digest

    @model_validator(mode="after")
    def validate_definition(self):
        metrics = {m.metric: m for m in self.metric_definitions}
        if len(metrics) != len(self.metric_definitions):
            raise ValueError("duplicate metric")
        if self.layer in {"SEMANTIC", "ADVERSARIAL"}:
            if self.rubric_artifact_id is None:
                raise ValueError("rubric required")
            for name, kind in [("score_bp", "INTEGER"), ("critical_violation", "BOOLEAN")]:
                if name not in metrics or metrics[name].value_type != kind:
                    raise ValueError("required semantic metrics missing")
        return self


class Threshold(Contract):
    metric: Text
    operator: Literal["EQ", "LE", "GE"]
    expected: Text
    unit: Text


class VerificationProfileCreate(Contract):
    project_id: UUID
    name: Text
    target_scope: Literal["TASK", "GOAL", "SKILL"]
    verifier_ref: Text
    verifier_digest: Digest
    required_layers: list[Layer] = Field(min_length=1, max_length=1)
    thresholds: list[Threshold] = Field(min_length=1, max_length=10000)
    required_evidence_kinds: list[Text] = Field(min_length=1, max_length=10000)
    applicability_rule_ref: Text

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.target_scope == "GOAL") != (self.required_layers == ["GLOBAL"]):
            raise ValueError("profile target/layer mismatch")
        keys = [(t.metric, t.operator, t.unit) for t in self.thresholds]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate threshold")
        if len(set(self.required_evidence_kinds)) != len(self.required_evidence_kinds):
            raise ValueError("duplicate evidence kind")
        return self


class VerificationProfileResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    version: PositiveInt
    content_digest: Digest
    config: VerificationProfileCreate


class MetricObservation(Contract):
    criterion_id: Text
    metric: Text
    value: Text | None
    status: Literal["OBSERVED", "MISSING", "INFRA_ERROR", "UNCERTAIN"]
    evidence_ids: list[UUID] = Field(max_length=10000)
    reason_code: Text

    @model_validator(mode="after")
    def validate_value(self):
        if self.status == "OBSERVED" and (self.value is None or not self.evidence_ids):
            raise ValueError("observed metric requires value and evidence")
        if self.status != "OBSERVED" and self.value is not None:
            raise ValueError("uncertain metric must not carry a value")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate evidence")
        return self


# Content v3 Observation 与 MetricObservation 同形；API 侧复用。
Observation = MetricObservation


class VerificationRunContent(Contract):
    """可信验证宿主提交的 VerificationRun 业务内容（无 id/digest）。"""

    project_id: UUID
    producer_activity_id: UUID
    producer_attempt_id: UUID
    subject_type: Literal["CANDIDATE", "SKILL_VERSION"]
    subject_id: UUID
    subject_digest: Digest
    verification_profile_id: UUID
    verifier_digest: Digest
    audit_round: int = Field(ge=1)
    layer: Layer
    input_digest: Digest
    environment_digest: Digest
    receipt_ids: list[UUID] = Field(min_length=1, max_length=10000)
    observations: list[Observation] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def validate_run(self):
        if len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise ValueError("duplicate receipt_id")
        keys = [(o.criterion_id, o.metric) for o in self.observations]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate observation")
        return self


class VerificationRunResource(VerificationRunContent):
    id: UUID
    created_at: datetime
    content_digest: Digest


class VerificationRunCreateRequest(Contract):
    """POST /internal/v1/verification-runs：租约 + Run 业务内容。"""

    lease: LeaseIdentity
    run: VerificationRunContent


class AssessmentCriterionResult(Contract):
    criterion_id: Text
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    evidence_ids: list[UUID] = Field(max_length=10000)
    reason: Text


class VerificationAssessmentResource(Contract):
    """Kernel 生成的 Assessment；不接受 worker 自报。"""

    id: UUID
    created_at: datetime
    run_id: UUID
    project_id: UUID
    subject_type: Literal["CANDIDATE", "SKILL_VERSION"]
    subject_id: UUID
    subject_digest: Digest
    verification_profile_id: UUID
    layer: Layer
    audit_round: int
    trust_revision: Epoch
    evaluator_digest: Digest
    criterion_results: list[AssessmentCriterionResult] = Field(min_length=1, max_length=10000)
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    content_digest: Digest


class VerificationObligationResource(Contract):
    """VerificationObligation 只读投影（doc/05 FG01；QUARANTINED 即对账 inbox 行）。"""

    id: UUID
    project_id: UUID
    activity_id: UUID
    attempt_id: UUID
    subject_type: Literal["CANDIDATE", "SKILL_VERSION"]
    subject_id: UUID
    profile_id: UUID
    layer: Layer
    audit_round: int = Field(ge=1)
    effect_ids: list[UUID] = Field(max_length=10000)
    invocation_ids: list[UUID] = Field(max_length=10000)
    status: Literal["OPEN", "ASSESSED", "QUARANTINED", "SUPERSEDED"]
    assessment_id: UUID | None = None
    superseded_by_obligation_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
