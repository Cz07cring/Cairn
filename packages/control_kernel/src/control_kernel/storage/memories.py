"""Memory 列表与 PROPOSED 创建；VERIFIED 晋升见 memory_index（INDEX_MEMORY outcome）。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.memory import MemoryCreate, MemoryResource
from ..protocols.runtime import PlanRejected
from .policies import ConfigurationVersions, TrustBlocked
from .projects import ProjectConflict


def _row_to_resource(row) -> MemoryResource:
    data = dict(row)
    return MemoryResource.model_validate(
        {
            **data,
            "source_evidence_ids": list(data["source_evidence_ids"] or []),
        }
    )


def list_memories(
    engine: Engine,
    subject: str,
    project_ids: list[str],
    *,
    project_id: UUID,
    kind: str | None,
    q: str | None,
    limit: int,
    after: UUID | None,
) -> list[MemoryResource]:
    """按项目 ACL 列出记忆；q 对 statement 做 ILIKE，kind 精确匹配。"""
    with engine.connect() as db:
        ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
        rows = db.execute(
            text(
                """SELECT * FROM memories WHERE project_id=:project
            AND (CAST(:kind AS text) IS NULL OR kind=CAST(:kind AS text))
            AND (CAST(:q AS text) IS NULL OR statement ILIKE ('%' || CAST(:q AS text) || '%'))
            AND (CAST(:after AS uuid) IS NULL OR (created_at,id)<(
              SELECT created_at,id FROM memories WHERE id=CAST(:after AS uuid)
                AND project_id=:project))
            ORDER BY created_at DESC, id DESC LIMIT :limit"""
            ),
            {
                "project": project_id,
                "kind": kind,
                "q": q,
                "after": after,
                "limit": limit,
            },
        ).mappings()
        return [_row_to_resource(row) for row in rows]


def create_memory(
    engine: Engine,
    subject: str,
    project_ids: list[str],
    key: str,
    body: MemoryCreate,
) -> MemoryResource:
    """幂等创建 PROPOSED 记忆；路由层暂限 admin（记忆服务身份未落地）。"""
    path = "/internal/v1/memories"
    request_scope = json.dumps(
        [str(body.project_id), subject, "POST", path, key], separators=(",", ":")
    )
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
        ConfigurationVersions.check_scope(db, body.project_id, subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": body.project_id},
        ).scalar_one()
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": request_scope},
        )
        old = (
            db.execute(
                text("SELECT body_digest,result FROM project_requests WHERE scope=:scope"),
                {"scope": request_scope},
            )
            .mappings()
            .first()
        )
        if old:
            if old["body_digest"] != request_digest:
                raise ProjectConflict()
            return MemoryResource.model_validate(old["result"])
        if trust != "OPEN":
            raise TrustBlocked()
        activity = db.execute(
            text(
                """SELECT id FROM activities
                WHERE project_id=:project AND id=:id"""
            ),
            {"project": body.project_id, "id": body.activity_id},
        ).first()
        if activity is None:
            raise PlanRejected("activity_id 不属于该项目或不存在")
        if body.supersedes_id is not None:
            prior = db.execute(
                text(
                    """SELECT id, status FROM memories
                    WHERE project_id=:project AND id=:id FOR UPDATE"""
                ),
                {"project": body.project_id, "id": body.supersedes_id},
            ).mappings().first()
            if prior is None:
                raise PlanRejected("supersedes_id 不属于该项目或不存在")
            if prior["status"] == "SUPERSEDED":
                raise PlanRejected("被替代的记忆已是 SUPERSEDED")
        for evidence_id in body.source_evidence_ids:
            exists = db.execute(
                text(
                    """SELECT 1 FROM artifacts
                    WHERE project_id=:project AND id=:id"""
                ),
                {"project": body.project_id, "id": evidence_id},
            ).first()
            if exists is None:
                raise PlanRejected("source_evidence_ids 含未知工件")
        now = datetime.now(UTC)
        row = (
            db.execute(
                text(
                    """INSERT INTO memories(
                  id,project_id,activity_id,kind,statement,source_evidence_ids,
                  confidence_bp,status,valid_from,supersedes_id)
                VALUES(
                  :id,:project,:activity,:kind,:statement,:evidence,
                  :confidence,'PROPOSED',:valid_from,:supersedes)
                RETURNING *"""
                ),
                {
                    "id": uuid4(),
                    "project": body.project_id,
                    "activity": body.activity_id,
                    "kind": body.kind,
                    "statement": body.statement,
                    "evidence": body.source_evidence_ids,
                    "confidence": body.confidence_bp,
                    "valid_from": now,
                    "supersedes": body.supersedes_id,
                },
            )
            .mappings()
            .one()
        )
        # 新版本冲突须显式标记旧行为 SUPERSEDED（同事务）
        if body.supersedes_id is not None:
            db.execute(
                text(
                    """UPDATE memories SET status='SUPERSEDED', updated_at=clock_timestamp()
                    WHERE id=:id AND project_id=:project AND status <> 'SUPERSEDED'"""
                ),
                {"id": body.supersedes_id, "project": body.project_id},
            )
        resource = _row_to_resource(row)
        db.execute(
            text(
                "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
            ),
            {
                "scope": request_scope,
                "digest": request_digest,
                "result": resource.model_dump_json(),
            },
        )
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'MEMORY_CREATED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": body.project_id,
                "payload": resource.model_dump_json(),
            },
        )
        return resource
