"""Project-scoped immutable configuration versions committed with request receipt and audit event."""

import json
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..domain.verification import validate_profile
from ..protocols.models import ModelProfileCreate, ModelProfileResource
from ..protocols.policies import PolicyCreate, PolicyResource
from ..protocols.verification import (
    VerificationProfileCreate,
    VerificationProfileResource,
    VerifierDefinition,
)
from .projects import ProjectConflict


class ScopeNotFound(Exception):
    pass


class InvalidConfiguration(Exception):
    pass


class TrustBlocked(Exception):
    pass


class ConfigurationVersions:
    def __init__(self, engine: Engine, kind: str):
        self.engine = engine
        # Closed mapping: SQL identifiers never come from requests.
        table, resource, path, event = {
            "verification": (
                "verification_profiles",
                VerificationProfileResource,
                "/api/v1/verification-profiles",
                "VERIFICATION_PROFILE_CREATED",
            ),
            "policy": ("policies", PolicyResource, "/api/v1/policies", "POLICY_CREATED"),
            "model": (
                "model_profiles",
                ModelProfileResource,
                "/api/v1/model-profiles",
                "MODEL_PROFILE_CREATED",
            ),
        }[kind]
        self.table, self.resource, self.path, self.event = table, resource, path, event

    @staticmethod
    def check_scope(db, project_id: UUID, subject: str, project_ids: list[str]) -> None:
        exists = db.execute(
            text("""SELECT id FROM projects p WHERE id=:id AND
            (id=ANY(CAST(:ids AS uuid[])) OR EXISTS
            (SELECT 1 FROM project_memberships m WHERE m.project_id=p.id AND m.subject=:subject))"""),
            {"id": project_id, "ids": project_ids, "subject": subject},
        ).first()
        if not exists:
            raise ScopeNotFound()

    def create(
        self,
        subject: str,
        project_ids: list[str],
        key: str,
        body: PolicyCreate | ModelProfileCreate | VerificationProfileCreate,
        digest: str,
        canonical: dict,
    ) -> PolicyResource | ModelProfileResource | VerificationProfileResource:
        request_scope = json.dumps(
            [str(body.project_id), subject, "POST", self.path, key], separators=(",", ":")
        )
        version_scope = json.dumps(
            [self.table, str(body.project_id), body.name], separators=(",", ":")
        )
        with self.engine.begin() as db:
            self.check_scope(db, body.project_id, subject, project_ids)
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
                return self.resource.model_validate(old["result"])
            if trust != "OPEN":
                raise TrustBlocked()
            if isinstance(body, VerificationProfileCreate):
                definition = db.execute(
                    text(
                        "SELECT content FROM verifier_definitions WHERE project_id=:project AND ref=:ref AND content_digest=:digest"
                    ),
                    {
                        "project": body.project_id,
                        "ref": body.verifier_ref,
                        "digest": body.verifier_digest,
                    },
                ).scalar_one_or_none()
                if definition is None:
                    raise InvalidConfiguration("unknown verifier")
                try:
                    validate_profile(VerifierDefinition.model_validate(definition), body)
                except ValueError as exc:
                    raise InvalidConfiguration("invalid verifier/profile binding") from exc
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": version_scope},
            )
            version = db.execute(
                text(
                    f"SELECT COALESCE(MAX(version),0)+1 FROM {self.table} WHERE project_id=:id AND name=:name"
                ),
                {"id": body.project_id, "name": body.name},
            ).scalar_one()
            returning = (
                "id,created_at,updated_at,version,content_digest,config,capability_status,probe_evidence_ids"
                if self.table == "model_profiles"
                else "id,created_at,updated_at,version,content_digest,config"
            )
            row = (
                db.execute(
                    text(f"""INSERT INTO {self.table}(id,project_id,name,version,content_digest,config)
                VALUES(:id,:project,:name,:version,:digest,CAST(:config AS jsonb)) RETURNING {returning}"""),
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
            result = self.resource.model_validate(dict(row))
            if isinstance(body, VerificationProfileCreate):
                db.execute(
                    text(
                        "INSERT INTO reference_edges(project_id,source_type,source_id,source_digest,target_type,target_id,target_digest) VALUES(:project,'VerificationProfile',:source,:digest,'VerifierDefinition',:target,:target_digest)"
                    ),
                    {
                        "project": body.project_id,
                        "source": str(result.id),
                        "digest": digest,
                        "target": body.verifier_ref,
                        "target_digest": body.verifier_digest,
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
                    "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,:kind,CAST(:payload AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "project": body.project_id,
                    "kind": self.event,
                    "payload": result.model_dump_json(),
                },
            )
            return result

    def list(
        self, project_id: UUID, subject: str, project_ids: list[str], limit: int, after: UUID | None
    ) -> list[PolicyResource | ModelProfileResource | VerificationProfileResource]:
        with self.engine.connect() as db:
            self.check_scope(db, project_id, subject, project_ids)
            columns = (
                "id,created_at,updated_at,version,content_digest,config,capability_status,probe_evidence_ids"
                if self.table == "model_profiles"
                else "id,created_at,updated_at,version,content_digest,config"
            )
            rows = db.execute(
                text(f"""SELECT {columns} FROM {self.table} WHERE project_id=:project
             AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(SELECT created_at,id FROM {self.table} WHERE id=CAST(:after AS uuid) AND project_id=:project))
             ORDER BY created_at,id LIMIT :limit"""),
                {"project": project_id, "after": after, "limit": limit},
            ).mappings()
            return [self.resource.model_validate(dict(row)) for row in rows]


class Policies(ConfigurationVersions):
    def __init__(self, engine: Engine):
        super().__init__(engine, "policy")


class ModelProfiles(ConfigurationVersions):
    def __init__(self, engine: Engine):
        super().__init__(engine, "model")


class VerificationProfiles(ConfigurationVersions):
    def __init__(self, engine: Engine):
        super().__init__(engine, "verification")
