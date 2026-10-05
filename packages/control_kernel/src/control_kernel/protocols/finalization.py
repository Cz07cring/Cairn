"""最终屏障与 ReleaseManifest 协议。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .audits import AuditCreate
from .goals import BarrierResource, Digest, Epoch
from .projects import Contract

# 再导出，便于路由层统一从 finalization 导入
__all__ = [
    "BarrierResource",
    "EvidenceExportRequest",
    "FinalizationRecoveryRequest",
    "FinalizeSuccessOutcome",
    "ReleaseManifestResource",
    "ReleaseValidity",
    "ReleaseView",
]


class EvidenceExportRequest(Contract):
    release_manifest_id: UUID
    trust_mode: Literal["INTERNAL_COPY", "OFFLINE_VERIFIABLE"]


class FinalizeSuccessOutcome(Contract):
    barrier_id: UUID
    candidate_manifest_id: UUID
    global_audits: list[AuditCreate] = Field(min_length=1, max_length=10000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10000)


class FinalizationRecoveryRequest(Contract):
    expected_state_revision: int = Field(ge=1)
    expected_plan_revision: int = Field(ge=1)
    barrier_id: UUID
    expected_write_epoch: Epoch
    action: Literal["REVERIFY", "REWORK"]
    reason: str = Field(min_length=1, max_length=2000)


class ReleaseManifestResource(Contract):
    id: UUID
    created_at: datetime
    project_id: UUID
    goal_id: UUID
    barrier_id: UUID
    write_epoch: Epoch
    goal_contract_digest: Digest
    plan_digest: Digest
    candidate_manifest_id: UUID
    verification_profile_ids: list[UUID]
    audit_ids: list[UUID]
    evidence_ids: list[UUID]
    content_digest: Digest


class ReleaseValidity(Contract):
    status: Literal["VALID", "INVALIDATED"]
    decision_ids: list[UUID] = Field(default_factory=list)


class ReleaseView(Contract):
    manifest: ReleaseManifestResource
    validity: ReleaseValidity
