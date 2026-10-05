"""Only project transactions are implemented here; no scheduler or execution writes."""

import hashlib
import json
from uuid import UUID, uuid4

from control_kernel.protocols.projects import ProjectCreate, ProjectResource
from sqlalchemy import Engine, text


class ProjectConflict(Exception):
    """The same request identity was reused for a different body."""


class Projects:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, subject: str, key: str, body: ProjectCreate) -> ProjectResource:
        payload = body.model_dump()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        scope = json.dumps([subject, "POST", "/api/v1/projects", key], separators=(",", ":"))
        with self.engine.begin() as db:
            # Serializes only this idempotency scope across API processes; no external IO in lock.
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": scope}
            )
            row = (
                db.execute(
                    text("SELECT body_digest, result FROM project_requests WHERE scope=:scope"),
                    {"scope": scope},
                )
                .mappings()
                .first()
            )
            if row:
                if row["body_digest"] != digest:
                    raise ProjectConflict()
                return ProjectResource.model_validate(row["result"])
            result = (
                db.execute(
                    text(
                        "INSERT INTO projects (id, name, repository_ref) VALUES (:id,:name,:repository_ref) RETURNING *"
                    ),
                    {"id": uuid4(), **payload},
                )
                .mappings()
                .one()
            )
            resource = ProjectResource.model_validate(dict(result))
            db.execute(
                text("INSERT INTO project_memberships (project_id, subject) VALUES (:id,:subject)"),
                {"id": resource.id, "subject": subject},
            )
            db.execute(
                text("INSERT INTO project_trust_states (project_id) VALUES (:id)"),
                {"id": resource.id},
            )
            db.execute(
                text(
                    "INSERT INTO project_requests(scope, body_digest, result) VALUES (:scope,:digest,CAST(:result AS jsonb))"
                ),
                {"scope": scope, "digest": digest, "result": resource.model_dump_json()},
            )
            db.execute(
                text(
                    "INSERT INTO project_events(id, project_id, kind, payload) VALUES (:id,:project,'PROJECT_CREATED',CAST(:payload AS jsonb))"
                ),
                {"id": uuid4(), "project": resource.id, "payload": resource.model_dump_json()},
            )
            return resource

    def list(
        self, subject: str, project_ids: list[UUID], limit: int, after: UUID | None
    ) -> list[ProjectResource]:
        # Membership or explicit issuer-assigned scope; roles alone never grant all projects.
        with self.engine.connect() as db:
            rows = db.execute(
                text("""SELECT p.* FROM projects p WHERE
                (EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id=p.id AND m.subject=:subject)
                 OR p.id=ANY(CAST(:ids AS uuid[])))
                AND (CAST(:after AS uuid) IS NULL OR (p.created_at,p.id) >
                    (SELECT created_at,id FROM projects WHERE id=CAST(:after AS uuid)))
                ORDER BY p.created_at,p.id LIMIT :limit"""),
                {
                    "subject": subject,
                    "ids": [str(i) for i in project_ids],
                    "limit": limit,
                    "after": str(after) if after else None,
                },
            ).mappings()
            return [ProjectResource.model_validate(dict(row)) for row in rows]
