"""Artifact catalog and byte verification. Ingestion is an INTERNAL trusted boundary.

HTTP collector 上传必须经 ingest_collector 校验租约 fencing；目录行只记录字节，
不等于 PASS 裁决或可信来源证明。
"""

import json
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID, uuid4

from evidence_ledger.objects import IntegrityError, S3Objects
from sqlalchemy import Engine, text

from ..protocols.artifacts import ArtifactResource
from ..protocols.runtime import LeaseRejected, WorkerForbidden
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked


class Artifacts:
    def __init__(self, engine: Engine, objects: S3Objects | None):
        self.engine, self.objects = engine, objects

    def ingest_raw(
        self, project_id: UUID, digest: str, source: BinaryIO, *, mime: str, producer_identity: str
    ) -> ArtifactResource:
        # Validate metadata before any I/O; the actual byte size replaces this placeholder.
        candidate = ArtifactResource(
            id=uuid4(),
            project_id=project_id,
            digest=digest,
            size_bytes=0,
            mime=mime,
            representation="RAW",
            derived_from_artifact_id=None,
            producer_identity=producer_identity,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        if self.objects is None:
            raise RuntimeError("object storage not configured")
        with self.engine.begin() as db:
            trust = db.execute(
                text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
                {"id": project_id},
            ).scalar_one_or_none()
            if trust is None:
                raise ScopeNotFound()
            if trust != "OPEN":
                raise TrustBlocked()
            receipt = self.objects.put(project_id, digest, source)
            candidate.size_bytes = receipt.size_bytes
            params = candidate.model_dump()
            record = (
                db.execute(
                    text("""INSERT INTO artifacts
                (id,project_id,digest,size_bytes,mime,representation,derived_from_artifact_id,producer_identity)
                VALUES (:id,:project_id,:digest,:size_bytes,:mime,:representation,:derived_from_artifact_id,:producer_identity)
                RETURNING *"""),
                    params,
                )
                .mappings()
                .one()
            )
            db.execute(
                text("""INSERT INTO project_events(id,project_id,kind,payload)
                VALUES (:id,:project,'ARTIFACT_REGISTERED',CAST(:payload AS jsonb))"""),
                {
                    "id": uuid4(),
                    "project": project_id,
                    "payload": json.dumps({"artifact_id": str(candidate.id), "digest": digest}),
                },
            )
            return ArtifactResource.model_validate(dict(record))
        # If DB commit fails, bytes may be orphaned. Never delete blindly: another
        # concurrent registration may reference them. Reference-aware GC is pending.

    def ingest_collector(
        self,
        project_id: UUID,
        digest: str,
        source: BinaryIO,
        *,
        mime: str,
        producer_identity: str,
        subject: str,
        activity_id: UUID,
        attempt_id: UUID,
        fencing_epoch: str,
    ) -> ArtifactResource:
        """仅在 ACTIVE attempt + fencing 匹配时允许 collector 入库。"""
        with self.engine.begin() as db:
            worker = (
                db.execute(
                    text("SELECT id FROM workers WHERE subject=:subject AND status='ACTIVE'"),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker is None:
                raise WorkerForbidden()
            attempt = (
                db.execute(
                    text("""SELECT * FROM activity_attempts
                    WHERE id=:id AND activity_id=:activity FOR SHARE"""),
                    {"id": attempt_id, "activity": activity_id},
                )
                .mappings()
                .first()
            )
            if attempt is None:
                raise ScopeNotFound()
            if attempt["worker_id"] != worker["id"]:
                raise WorkerForbidden()
            if str(attempt["fencing_epoch"]) != fencing_epoch:
                raise LeaseRejected("FENCING_REJECTED", "fencing epoch 不匹配")
            if attempt["status"] != "ACTIVE":
                raise LeaseRejected("INVALID_STATE", "attempt 已非 ACTIVE")
            activity = (
                db.execute(
                    text("SELECT project_id FROM activities WHERE id=:id"),
                    {"id": activity_id},
                )
                .mappings()
                .first()
            )
            if activity is None or activity["project_id"] != project_id:
                raise LeaseRejected("INVALID_REQUEST", "活动与项目不一致")
            now = datetime.now(UTC)
            expires = attempt["lease_expires_at"]
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= now:
                raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能上传")

        return self.ingest_raw(
            project_id, digest, source, mime=mime, producer_identity=producer_identity
        )

    def get(self, artifact_id: UUID, subject: str, project_ids: list[str]) -> ArtifactResource:
        with self.engine.connect() as db:
            row = (
                db.execute(text("SELECT * FROM artifacts WHERE id=:id"), {"id": artifact_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            return ArtifactResource.model_validate(dict(row))

    def get_for_worker(self, artifact_id: UUID, subject: str) -> ArtifactResource:
        """已登记 ACTIVE worker 读取输入工件；不依赖 JWT project_ids。

        仍要求工件属于该 worker 当前 ACTIVE attempt 所在项目，避免跨项目扫库。
        """
        with self.engine.connect() as db:
            worker = (
                db.execute(
                    text("SELECT id FROM workers WHERE subject=:subject AND status='ACTIVE'"),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker is None:
                raise WorkerForbidden()
            row = (
                db.execute(text("SELECT * FROM artifacts WHERE id=:id"), {"id": artifact_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            allowed = db.execute(
                text(
                    """SELECT 1 FROM activity_attempts aa
                    INNER JOIN activities a ON a.id = aa.activity_id
                    WHERE aa.worker_id=:worker AND aa.status='ACTIVE'
                      AND a.project_id=:project
                    LIMIT 1"""
                ),
                {"worker": worker["id"], "project": row["project_id"]},
            ).first()
            if allowed is None:
                raise ScopeNotFound()
            return ArtifactResource.model_validate(dict(row))

    def content(self, record: ArtifactResource) -> bytes:
        if self.objects is None:
            raise RuntimeError("object storage not configured")
        content = self.objects.read(record.project_id, record.digest)
        if len(content) != record.size_bytes:
            raise IntegrityError("catalog size mismatch")
        return content
