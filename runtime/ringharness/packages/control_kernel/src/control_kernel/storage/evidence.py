"""EvidenceLedger：从 TrustedReceipt + Effect 生成不可变 EvidenceEnvelope。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from evidence_ledger.content import encode
from sqlalchemy import Connection, Engine, text

from ..protocols.effects import TrustedReceipt
from ..protocols.evidence import EvidenceEnvelopeResource
from .policies import ConfigurationVersions, ScopeNotFound


def _ms_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _content_digest(content: dict) -> str:
    canonical = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": "EvidenceEnvelope",
                "content": content,
                "reference_bindings": [],
            },
            separators=(",", ":"),
        )
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _from_row(row) -> EvidenceEnvelopeResource:
    data = dict(row)
    return EvidenceEnvelopeResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "schema_version": data["schema_version"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "task_id": data["task_id"],
            "producer_activity_id": data["producer_activity_id"],
            "producer_attempt_id": data["producer_attempt_id"],
            "effect_id": data["effect_id"],
            "receipt_id": data["receipt_id"],
            "contract_digest": data["contract_digest"],
            "candidate_manifest_id": data["candidate_manifest_id"],
            "verification_profile_id": data["verification_profile_id"],
            "command_argv": list(data["command_argv"] or []),
            "input_digest": data["input_digest"],
            "environment_digest": data["environment_digest"],
            "started_at": data["started_at"],
            "finished_at": data["finished_at"],
            "exit_code": data["exit_code"],
            "signal": data["signal"],
            "timed_out": data["timed_out"],
            "artifact_ids": list(data["artifact_ids"] or []),
            "producer_identity": data["producer_identity"],
            "content_digest": data["content_digest"],
        }
    )


def ingest_from_receipt(
    db: Connection,
    *,
    effect: dict,
    body: TrustedReceipt,
    producer_identity: str,
) -> EvidenceEnvelopeResource:
    """同事务写入信封；同 effect+receipt 幂等返回已有行。模型不得调用此入口。"""
    existing = (
        db.execute(
            text(
                """SELECT * FROM evidence_envelopes
                WHERE effect_id=:effect AND receipt_id=:receipt"""
            ),
            {"effect": effect["id"], "receipt": body.receipt_id},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return _from_row(existing)

    activity = (
        db.execute(
            text("SELECT goal_id, task_id FROM activities WHERE id=:id"),
            {"id": body.producer_activity_id},
        )
        .mappings()
        .one()
    )
    input_row = (
        db.execute(
            text("SELECT digest FROM artifacts WHERE id=:id"),
            {"id": effect["input_artifact_id"]},
        )
        .mappings()
        .first()
    )
    if input_row is None:
        raise ScopeNotFound()
    contract_digest = None
    if activity["goal_id"] is not None:
        contract_digest = db.execute(
            text("SELECT contract_digest FROM goals WHERE id=:id"),
            {"id": activity["goal_id"]},
        ).scalar()

    env_payload = {
        "tool_ref": effect["tool_ref"],
        "replay_class": effect["replay_class"],
        "scope": effect["scope"],
        "intent_revision": effect["intent_revision"],
    }
    environment_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(env_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    artifact_ids: list[UUID] = []
    for item in (
        body.stdout_artifact_id,
        body.stderr_artifact_id,
        *list(body.result_artifact_ids or []),
    ):
        if item is not None and item not in artifact_ids:
            artifact_ids.append(item)
    # set 语义：入库前排序，与 Content v3 x-order=set 一致
    artifact_ids = sorted(artifact_ids, key=lambda u: str(u))
    command_argv = ["execution_broker", effect["tool_ref"]]
    content = {
        "project_id": str(effect["project_id"]),
        "goal_id": str(activity["goal_id"]) if activity["goal_id"] else None,
        "task_id": str(activity["task_id"]) if activity["task_id"] else None,
        "producer_activity_id": str(body.producer_activity_id),
        "producer_attempt_id": str(body.producer_attempt_id),
        "effect_id": str(effect["id"]),
        "receipt_id": str(body.receipt_id),
        "contract_digest": contract_digest,
        "candidate_manifest_id": None,
        "verification_profile_id": None,
        "command_argv": command_argv,
        "input_digest": input_row["digest"],
        "environment_digest": environment_digest,
        "started_at": _ms_z(body.started_at),
        "finished_at": _ms_z(body.finished_at),
        "exit_code": body.exit_code,
        "signal": body.signal,
        "timed_out": body.timed_out,
        "artifact_ids": [str(i) for i in artifact_ids],
        "producer_identity": producer_identity,
    }
    digest = _content_digest(content)
    envelope_id = uuid4()
    row = (
        db.execute(
            text(
                """INSERT INTO evidence_envelopes(
                  id,project_id,goal_id,task_id,producer_activity_id,producer_attempt_id,
                  effect_id,receipt_id,contract_digest,candidate_manifest_id,
                  verification_profile_id,command_argv,input_digest,environment_digest,
                  started_at,finished_at,exit_code,signal,timed_out,artifact_ids,
                  producer_identity,content_digest,schema_version)
                VALUES(
                  :id,:project,:goal,:task,:activity,:attempt,
                  :effect,:receipt,:contract,NULL,
                  NULL,:argv,:input,:env,
                  :started,:finished,:exit,:signal,:timed,:artifacts,
                  :identity,:digest,3)
                RETURNING *"""
            ),
            {
                "id": envelope_id,
                "project": effect["project_id"],
                "goal": activity["goal_id"],
                "task": activity["task_id"],
                "activity": body.producer_activity_id,
                "attempt": body.producer_attempt_id,
                "effect": effect["id"],
                "receipt": body.receipt_id,
                "contract": contract_digest,
                "argv": command_argv,
                "input": input_row["digest"],
                "env": environment_digest,
                "started": body.started_at,
                "finished": body.finished_at,
                "exit": body.exit_code,
                "signal": body.signal,
                "timed": body.timed_out,
                "artifacts": artifact_ids,
                "identity": producer_identity,
                "digest": digest,
            },
        )
        .mappings()
        .one()
    )
    if activity["goal_id"] is not None:
        from .events import append_goal_event

        append_goal_event(
            db,
            project_id=effect["project_id"],
            goal_id=activity["goal_id"],
            event_type="EVIDENCE_CREATED",
            entity_id=envelope_id,
            entity_state_revision=None,
            resource_type="EVIDENCE",
        )
    return _from_row(row)


def list_for_task(
    engine: Engine,
    task_id: UUID,
    subject: str,
    project_ids: list[str],
    limit: int,
    after: UUID | None,
) -> list[EvidenceEnvelopeResource]:
    with engine.connect() as db:
        task = (
            db.execute(text("SELECT project_id FROM tasks WHERE id=:id"), {"id": task_id})
            .mappings()
            .first()
        )
        if task is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, task["project_id"], subject, project_ids)
        rows = db.execute(
            text(
                """SELECT * FROM evidence_envelopes WHERE task_id=:task
            AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
              SELECT created_at,id FROM evidence_envelopes WHERE id=CAST(:after AS uuid)
                AND task_id=:task))
            ORDER BY created_at,id LIMIT :limit"""
            ),
            {"task": task_id, "after": after, "limit": limit},
        ).mappings()
        return [_from_row(row) for row in rows]
