"""CandidateManifest / ProtectedBaseline / EXECUTE outcome。"""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from .goals import Digest, PositiveInt, Text
from .projects import Contract
from .runtime import LeaseIdentity


class CandidateFileEntry(Contract):
    path: Text = Field(min_length=1, max_length=10000)
    digest: Digest
    mode: Text = Field(min_length=1, max_length=10000)


class SubmoduleEntry(Contract):
    path: Text = Field(min_length=1, max_length=10000)
    commit: Text = Field(min_length=1, max_length=10000)
    content_digest: Digest


class LfsObjectEntry(Contract):
    path: Text = Field(min_length=1, max_length=10000)
    oid: Text = Field(min_length=1, max_length=10000)
    content_digest: Digest


class WorkspaceSnapshot(Contract):
    """冻结快照工件内容；由受信采集写入对象仓，seal 只读。"""

    files: list[CandidateFileEntry] = Field(max_length=10000)
    git_commit: str | None = None
    dependency_lock_digests: list[Digest] = Field(default_factory=list, max_length=10000)
    submodules: list[SubmoduleEntry] = Field(default_factory=list, max_length=10000)
    lfs_objects: list[LfsObjectEntry] = Field(default_factory=list, max_length=10000)
    image_digests: list[Text] = Field(default_factory=list, max_length=10000)


class CandidateSealRequest(Contract):
    lease: LeaseIdentity
    workspace_snapshot_artifact_id: UUID
    verification_profile_ids: list[UUID] = Field(min_length=1, max_length=10000)


class CandidateManifestResource(Contract):
    id: UUID
    created_at: datetime
    project_id: UUID
    goal_id: UUID
    task_id: UUID | None
    activity_id: UUID
    protected_baseline_digest: Digest
    git_commit: str | None
    files: list[CandidateFileEntry]
    dependency_lock_digests: list[Digest]
    submodules: list[SubmoduleEntry]
    lfs_objects: list[LfsObjectEntry]
    image_digests: list[Text]
    verification_profile_ids: list[UUID]
    content_digest: Digest
    workspace_snapshot_artifact_id: UUID


class ExecuteSuccessOutcome(Contract):
    candidate_manifest_id: UUID
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10000)


class ProtectedBaselineResource(Contract):
    id: UUID
    created_at: datetime
    project_id: UUID
    goal_id: UUID
    goal_contract_revision: PositiveInt
    content_digest: Digest
