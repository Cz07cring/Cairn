"""Skill 版本与 SkillSet 持久化；VALIDATE_SKILL Activity 未接通前，验收记录仅受信 fixture 写入。"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.skills import (
    SkillActivate,
    SkillCreate,
    SkillRevoke,
    SkillSetCreate,
    SkillSetResource,
    SkillValidationRecordResource,
    SkillVersionResource,
)
from .policies import ConfigurationVersions, InvalidConfiguration, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict


def assert_skill_set_versions_active(
    db, *, project_id: UUID, skill_set_id: UUID
) -> None:
    """新绑定 Goal/合同前：SkillSet 内全部 skill_version 须仍为 ACTIVE（doc/05 §3.11）。"""
    row = (
        db.execute(
            text(
                """SELECT config FROM skill_sets
                WHERE project_id=:p AND id=:id"""
            ),
            {"p": project_id, "id": skill_set_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise InvalidConfiguration("missing skill_sets")
    config = row["config"]
    if isinstance(config, str):
        config = json.loads(config)
    version_ids = list(config.get("skill_version_ids") or [])
    for version_id in version_ids:
        status = db.execute(
            text(
                """SELECT status FROM skill_versions
                WHERE project_id=:p AND id=:id"""
            ),
            {"p": project_id, "id": version_id},
        ).scalar_one_or_none()
        if status != "ACTIVE":
            raise InvalidConfiguration(
                "SkillSet 含非 ACTIVE SkillVersion，禁止新绑定"
                f"（version={version_id} status={status}）"
            )


class Skills:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(
        self,
        subject: str,
        project_ids: list[str],
        key: str,
        body: SkillCreate,
        digest: str,
        canonical: dict,
    ) -> SkillVersionResource:
        path = "/api/v1/skills"
        request_scope = json.dumps(
            [str(body.project_id), subject, "POST", path, key], separators=(",", ":")
        )
        version_scope = json.dumps(
            ["skill_versions", str(body.project_id), body.name], separators=(",", ":")
        )
        with self.engine.begin() as db:
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
                if old["body_digest"] != digest:
                    raise ProjectConflict()
                return SkillVersionResource.model_validate(old["result"])
            if trust != "OPEN":
                raise TrustBlocked()

            # M4：Skill.required_tools 对照 ToolCapabilityManifest + 危险组合/fixture
            from ..domain.tool_capability_manifest import (
                ToolCapabilityRejected,
                assert_skill_required_tools_admissible,
            )

            try:
                assert_skill_required_tools_admissible(list(body.required_tools or []))
            except ToolCapabilityRejected as err:
                raise InvalidConfiguration(f"{err.code}: {err.message}") from err

            artifact = db.execute(
                text("SELECT id FROM artifacts WHERE project_id=:project AND id=:id"),
                {"project": body.project_id, "id": body.content_artifact_id},
            ).first()
            if artifact is None:
                raise InvalidConfiguration("missing skill artifact")
            profile = (
                db.execute(
                    text(
                        "SELECT config FROM verification_profiles WHERE project_id=:project AND id=:id"
                    ),
                    {"project": body.project_id, "id": body.verification_profile_id},
                )
                .mappings()
                .first()
            )
            if profile is None or profile["config"].get("target_scope") != "SKILL":
                raise InvalidConfiguration("skill verification profile required")
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": version_scope},
            )
            prior = (
                db.execute(
                    text(
                        """SELECT skill_id,version FROM skill_versions
                        WHERE project_id=:project AND name=:name ORDER BY version DESC LIMIT 1"""
                    ),
                    {"project": body.project_id, "name": body.name},
                )
                .mappings()
                .first()
            )
            skill_id = prior["skill_id"] if prior else uuid4()
            version = (prior["version"] + 1) if prior else 1
            row = (
                db.execute(
                    text("""INSERT INTO skill_versions
                    (id,skill_id,project_id,name,version,content_digest,config,status,audit_id,revocation_reason)
                    VALUES(:id,:skill,:project,:name,:version,:digest,CAST(:config AS jsonb),'CANDIDATE',NULL,NULL)
                    RETURNING id,skill_id,created_at,updated_at,version,content_digest,config,status,audit_id,revocation_reason"""),
                    {
                        "id": uuid4(),
                        "skill": skill_id,
                        "project": body.project_id,
                        "name": body.name,
                        "version": version,
                        "digest": digest,
                        "config": json.dumps(canonical),
                    },
                )
                .mappings()
                .one()
            )
            result = SkillVersionResource.model_validate(dict(row))
            db.execute(
                text(
                    """INSERT INTO reference_edges
                    (project_id,source_type,source_id,source_digest,target_type,target_id,target_digest)
                    VALUES(:project,'SkillVersion',:source,:digest,'Artifact',:artifact,:artifact_digest)"""
                ),
                {
                    "project": body.project_id,
                    "source": str(result.id),
                    "digest": digest,
                    "artifact": str(body.content_artifact_id),
                    # 工件 digest 从目录读取，边目标摘要必须等于真实字节地址。
                    "artifact_digest": db.execute(
                        text("SELECT digest FROM artifacts WHERE id=:id"),
                        {"id": body.content_artifact_id},
                    ).scalar_one(),
                },
            )
            db.execute(
                text(
                    """INSERT INTO reference_edges
                    (project_id,source_type,source_id,source_digest,target_type,target_id,target_digest)
                    VALUES(:project,'SkillVersion',:source,:digest,'VerificationProfile',:profile,:profile_digest)"""
                ),
                {
                    "project": body.project_id,
                    "source": str(result.id),
                    "digest": digest,
                    "profile": str(body.verification_profile_id),
                    "profile_digest": db.execute(
                        text("SELECT content_digest FROM verification_profiles WHERE id=:id"),
                        {"id": body.verification_profile_id},
                    ).scalar_one(),
                },
            )
            db.execute(
                text(
                    "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
                ),
                {"scope": request_scope, "digest": digest, "result": result.model_dump_json()},
            )
            db.execute(
                text(
                    "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'SKILL_VERSION_CREATED',CAST(:payload AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "project": body.project_id,
                    "payload": result.model_dump_json(),
                },
            )
            return result

    def list_versions(
        self,
        project_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        after: UUID | None,
        status: str | None = None,
    ) -> list[SkillVersionResource]:
        with self.engine.connect() as db:
            ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
            rows = db.execute(
                text("""SELECT id,skill_id,created_at,updated_at,version,content_digest,config,status,audit_id,revocation_reason
                FROM skill_versions WHERE project_id=:project
                AND (CAST(:status AS text) IS NULL OR status=CAST(:status AS text))
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                    SELECT created_at,id FROM skill_versions WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""),
                {"project": project_id, "after": after, "limit": limit, "status": status},
            ).mappings()
            return [SkillVersionResource.model_validate(dict(row)) for row in rows]

    def activate(
        self,
        version_id: UUID,
        subject: str,
        project_ids: list[str],
        key: str,
        body: SkillActivate,
    ) -> SkillVersionResource:
        request_scope = json.dumps(
            [str(version_id), subject, "POST", f"/api/v1/skills/{version_id}/activate", key],
            separators=(",", ":"),
        )
        with self.engine.begin() as db:
            row = (
                db.execute(
                    text("SELECT * FROM skill_versions WHERE id=:id FOR UPDATE"),
                    {"id": version_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            trust = db.execute(
                text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
                {"id": row["project_id"]},
            ).scalar_one()
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": request_scope},
            )
            digest = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            body_digest = hashlib.sha256(digest.encode()).hexdigest()
            old = (
                db.execute(
                    text("SELECT body_digest,result FROM project_requests WHERE scope=:scope"),
                    {"scope": request_scope},
                )
                .mappings()
                .first()
            )
            if old:
                if old["body_digest"] != body_digest:
                    raise ProjectConflict()
                return SkillVersionResource.model_validate(old["result"])
            if trust != "OPEN":
                raise TrustBlocked()
            if row["status"] not in {"CANDIDATE", "VALIDATING"}:
                raise InvalidConfiguration("skill not activatable")
            audit = (
                db.execute(
                    text(
                        """SELECT * FROM skill_validation_records
                        WHERE id=:id AND project_id=:project AND subject_skill_version_id=:version"""
                    ),
                    {"id": body.audit_id, "project": row["project_id"], "version": version_id},
                )
                .mappings()
                .first()
            )
            if audit is None:
                raise InvalidConfiguration("validation record missing")
            if (
                audit["verdict"] != "PASS"
                or audit["subject_digest"] != row["content_digest"]
                or str(audit["verification_profile_id"])
                != str(row["config"]["verification_profile_id"])
            ):
                raise InvalidConfiguration("validation record not usable")
            content = audit["content"]
            required = {c["id"] for c in row["config"]["acceptance"] if c.get("required")}
            results = {
                c["criterion_id"]: c["verdict"] for c in content.get("criterion_results", [])
            }
            if any(results.get(cid) != "PASS" for cid in required):
                raise InvalidConfiguration("required criteria not passed")
            skill_lock = json.dumps(
                ["skill", str(row["project_id"]), str(row["skill_id"])], separators=(",", ":")
            )
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": skill_lock},
            )
            updated = (
                db.execute(
                    text("""UPDATE skill_versions SET status='ACTIVE', audit_id=:audit,
                    revocation_reason=NULL, updated_at=clock_timestamp()
                    WHERE id=:id RETURNING id,skill_id,created_at,updated_at,version,content_digest,config,status,audit_id,revocation_reason"""),
                    {"id": version_id, "audit": body.audit_id},
                )
                .mappings()
                .one()
            )
            result = SkillVersionResource.model_validate(dict(updated))
            db.execute(
                text(
                    "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
                ),
                {
                    "scope": request_scope,
                    "digest": body_digest,
                    "result": result.model_dump_json(),
                },
            )
            db.execute(
                text(
                    "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'SKILL_VERSION_ACTIVATED',CAST(:payload AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "project": row["project_id"],
                    "payload": result.model_dump_json(),
                },
            )
            return result

    def revoke(
        self,
        version_id: UUID,
        subject: str,
        project_ids: list[str],
        key: str,
        body: SkillRevoke,
    ) -> SkillVersionResource:
        request_scope = json.dumps(
            [str(version_id), subject, "POST", f"/api/v1/skills/{version_id}/revoke", key],
            separators=(",", ":"),
        )
        with self.engine.begin() as db:
            row = (
                db.execute(
                    text("SELECT * FROM skill_versions WHERE id=:id FOR UPDATE"),
                    {"id": version_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": request_scope},
            )
            digest = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            body_digest = hashlib.sha256(digest.encode()).hexdigest()
            old = (
                db.execute(
                    text("SELECT body_digest,result FROM project_requests WHERE scope=:scope"),
                    {"scope": request_scope},
                )
                .mappings()
                .first()
            )
            if old:
                if old["body_digest"] != body_digest:
                    raise ProjectConflict()
                return SkillVersionResource.model_validate(old["result"])
            if row["status"] == "REVOKED":
                return SkillVersionResource.model_validate(dict(row))
            skill_lock = json.dumps(
                ["skill", str(row["project_id"]), str(row["skill_id"])], separators=(",", ":")
            )
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": skill_lock},
            )
            updated = (
                db.execute(
                    text("""UPDATE skill_versions SET status='REVOKED', revocation_reason=:reason,
                    updated_at=clock_timestamp() WHERE id=:id
                    RETURNING id,skill_id,created_at,updated_at,version,content_digest,config,status,audit_id,revocation_reason"""),
                    {"id": version_id, "reason": body.reason},
                )
                .mappings()
                .one()
            )
            result = SkillVersionResource.model_validate(dict(updated))
            db.execute(
                text(
                    "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
                ),
                {
                    "scope": request_scope,
                    "digest": body_digest,
                    "result": result.model_dump_json(),
                },
            )
            db.execute(
                text(
                    "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'SKILL_VERSION_REVOKED',CAST(:payload AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "project": row["project_id"],
                    "payload": result.model_dump_json(),
                },
            )
            return result

    def list_validations(
        self,
        version_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        after: UUID | None,
    ) -> list[SkillValidationRecordResource]:
        with self.engine.connect() as db:
            row = (
                db.execute(
                    text("SELECT project_id FROM skill_versions WHERE id=:id"),
                    {"id": version_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            rows = db.execute(
                text("""SELECT id,created_at,updated_at,content_digest,content
                FROM skill_validation_records
                WHERE project_id=:project AND subject_skill_version_id=:version
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                    SELECT created_at,id FROM skill_validation_records
                    WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""),
                {
                    "project": row["project_id"],
                    "version": version_id,
                    "after": after,
                    "limit": limit,
                },
            ).mappings()
            results = []
            for item in rows:
                payload = dict(item["content"])
                payload.update(
                    {
                        "id": item["id"],
                        "created_at": item["created_at"],
                        "updated_at": item["updated_at"],
                        "content_digest": item["content_digest"],
                    }
                )
                results.append(SkillValidationRecordResource.model_validate(payload))
            return results


class SkillSets:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(
        self,
        subject: str,
        project_ids: list[str],
        key: str,
        body: SkillSetCreate,
        digest: str,
        canonical: dict,
    ) -> SkillSetResource:
        path = "/api/v1/skill-sets"
        request_scope = json.dumps(
            [str(body.project_id), subject, "POST", path, key], separators=(",", ":")
        )
        version_scope = json.dumps(
            ["skill_sets", str(body.project_id), body.name], separators=(",", ":")
        )
        with self.engine.begin() as db:
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
                if old["body_digest"] != digest:
                    raise ProjectConflict()
                return SkillSetResource.model_validate(old["result"])
            if trust != "OPEN":
                raise TrustBlocked()
            for version_id in body.skill_version_ids:
                status = db.execute(
                    text("SELECT status FROM skill_versions WHERE project_id=:project AND id=:id"),
                    {"project": body.project_id, "id": version_id},
                ).scalar_one_or_none()
                if status != "ACTIVE":
                    raise InvalidConfiguration("skill set requires ACTIVE versions")
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": version_scope},
            )
            version = db.execute(
                text(
                    "SELECT COALESCE(MAX(version),0)+1 FROM skill_sets WHERE project_id=:id AND name=:name"
                ),
                {"id": body.project_id, "name": body.name},
            ).scalar_one()
            row = (
                db.execute(
                    text("""INSERT INTO skill_sets(id,project_id,name,version,content_digest,config)
                    VALUES(:id,:project,:name,:version,:digest,CAST(:config AS jsonb))
                    RETURNING id,created_at,updated_at,version,content_digest,config"""),
                    {
                        "id": uuid4(),
                        "project": body.project_id,
                        "name": body.name,
                        "version": version,
                        "digest": digest,
                        "config": json.dumps(canonical),
                    },
                )
                .mappings()
                .one()
            )
            result = SkillSetResource.model_validate(dict(row))
            for version_id in body.skill_version_ids:
                skill_digest = db.execute(
                    text("SELECT content_digest FROM skill_versions WHERE id=:id"),
                    {"id": version_id},
                ).scalar_one()
                db.execute(
                    text(
                        """INSERT INTO reference_edges
                        (project_id,source_type,source_id,source_digest,target_type,target_id,target_digest)
                        VALUES(:project,'SkillSet',:source,:digest,'SkillVersion',:target,:target_digest)"""
                    ),
                    {
                        "project": body.project_id,
                        "source": str(result.id),
                        "digest": digest,
                        "target": str(version_id),
                        "target_digest": skill_digest,
                    },
                )
            db.execute(
                text(
                    "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
                ),
                {"scope": request_scope, "digest": digest, "result": result.model_dump_json()},
            )
            db.execute(
                text(
                    "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'SKILL_SET_CREATED',CAST(:payload AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "project": body.project_id,
                    "payload": result.model_dump_json(),
                },
            )
            return result

    def list_sets(
        self, project_id: UUID, subject: str, project_ids: list[str], limit: int, after: UUID | None
    ) -> list[SkillSetResource]:
        with self.engine.connect() as db:
            ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
            rows = db.execute(
                text("""SELECT id,created_at,updated_at,version,content_digest,config FROM skill_sets
                WHERE project_id=:project
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                    SELECT created_at,id FROM skill_sets WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""),
                {"project": project_id, "after": after, "limit": limit},
            ).mappings()
            return [SkillSetResource.model_validate(dict(row)) for row in rows]

    # ConfigurationHTTP 约定仓库方法名为 list。
    list = list_sets
