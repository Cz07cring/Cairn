"""Checkpoint 采纳与列表；PLAN 允许，须绑定 context。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from evidence_ledger.content import encode
from sqlalchemy import Engine, text

from ..protocols.runtime import (
    Checkpoint,
    CheckpointProposal,
    CheckpointResource,
    LeaseRejected,
    PlanRejected,
)
from .effects import RoleToolForbidden, _require_owner_attempt
from .policies import ScopeNotFound


def _checkpoint_content_digest(body: Checkpoint) -> str:
    # session_ref 不进 Content v3 摘要（canonicalization 排除项）。
    content = {
        "activity_id": str(body.activity_id),
        "attempt_id": str(body.attempt_id),
        "kind": body.kind,
        "context_digest": body.context_digest,
        "completed_step_ids": [str(i) for i in body.completed_step_ids],
        "next_step_id": str(body.next_step_id) if body.next_step_id else None,
        "candidate_manifest_id": (
            str(body.candidate_manifest_id) if body.candidate_manifest_id else None
        ),
        "workspace_manifest_digest": body.workspace_manifest_digest,
        "artifact_ids": sorted(str(i) for i in body.artifact_ids),
        "effect_ids": sorted(str(i) for i in body.effect_ids),
    }
    canonical = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": "Checkpoint",
                "content": content,
                "reference_bindings": [],
            }
        )
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _from_row(row) -> CheckpointResource:
    return CheckpointResource.model_validate(
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "activity_id": row["activity_id"],
            "attempt_id": row["attempt_id"],
            "kind": row["kind"],
            "context_digest": row["context_digest"],
            "completed_step_ids": list(row["completed_step_ids"] or []),
            "next_step_id": row["next_step_id"],
            "candidate_manifest_id": row["candidate_manifest_id"],
            "workspace_manifest_digest": row["workspace_manifest_digest"],
            "artifact_ids": list(row["artifact_ids"] or []),
            "effect_ids": list(row["effect_ids"] or []),
            "session_ref": row["session_ref"],
            "content_digest": row["content_digest"],
            "created_at": row["created_at"],
            "schema_version": 3,
        }
    )


def _validate_plan_shape(body: Checkpoint) -> None:
    # B03：PLAN checkpoint 不得声称工作区/步骤/效果。
    if body.workspace_manifest_digest is not None:
        raise PlanRejected("PLAN checkpoint 不得带 workspace_manifest_digest")
    if body.candidate_manifest_id is not None:
        raise PlanRejected("PLAN checkpoint 不得带 candidate_manifest_id")
    if body.next_step_id is not None:
        raise PlanRejected("PLAN checkpoint 不得带 next_step_id")
    if body.completed_step_ids:
        raise PlanRejected("PLAN checkpoint 的 completed_step_ids 必须为空")
    if body.effect_ids:
        raise PlanRejected("PLAN checkpoint 的 effect_ids 必须为空")


def accept_checkpoint(
    engine: Engine, subject: str, activity_id: UUID, body: CheckpointProposal
) -> CheckpointResource:
    cp = body.checkpoint
    with engine.begin() as db:
        activity, attempt = _require_owner_attempt(db, subject, activity_id, body.lease)
        if cp.activity_id != activity_id or cp.attempt_id != attempt["id"]:
            raise LeaseRejected("INVALID_REQUEST", "checkpoint 与租约活动/attempt 不一致")
        if cp.kind != activity["kind"]:
            raise PlanRejected("checkpoint.kind 必须与 Activity.kind 一致")
        if attempt["context_digest"] is None:
            raise PlanRejected("attempt 尚未绑定 context，不能保存 checkpoint")
        if cp.context_digest != attempt["context_digest"]:
            raise PlanRejected("context_digest 与 attempt 绑定不一致")
        if activity["kind"] == "PLAN":
            _validate_plan_shape(cp)
        elif activity["kind"] not in (
            "EXECUTE",
            "AUDIT",
            "INTEGRATE",
            "RECONCILE",
            "FINALIZE",
            "PROBE_MODEL",
            "VALIDATE_SKILL",
            "INDEX_MEMORY",
            "EXPORT_EVIDENCE",
        ):
            raise RoleToolForbidden()

        for artifact_id in cp.artifact_ids:
            row = db.execute(
                text(
                    """SELECT 1 FROM artifacts
                    WHERE project_id=:project AND id=:id"""
                ),
                {"project": activity["project_id"], "id": artifact_id},
            ).first()
            if row is None:
                raise PlanRejected("artifact 未持久化或不属于本项目")

        for effect_id in cp.effect_ids:
            effect = (
                db.execute(
                    text(
                        """SELECT status FROM effect_intents
                        WHERE project_id=:project AND id=:id AND activity_id=:activity"""
                    ),
                    {
                        "project": activity["project_id"],
                        "id": effect_id,
                        "activity": activity_id,
                    },
                )
                .mappings()
                .first()
            )
            if effect is None:
                raise PlanRejected("effect 未持久化或不属于本活动")
            if effect["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"):
                raise PlanRejected("checkpoint 引用的 effect 尚未到达可保留终态")

        for step_id in cp.completed_step_ids:
            step = db.execute(
                text(
                    """SELECT 1 FROM activity_steps
                    WHERE project_id=:project AND id=:id AND activity_id=:activity"""
                ),
                {
                    "project": activity["project_id"],
                    "id": step_id,
                    "activity": activity_id,
                },
            ).first()
            if step is None:
                raise PlanRejected("completed_step_ids 含未登记步骤")

        if cp.next_step_id is not None:
            nxt = db.execute(
                text(
                    """SELECT 1 FROM activity_steps
                    WHERE project_id=:project AND id=:id AND activity_id=:activity"""
                ),
                {
                    "project": activity["project_id"],
                    "id": cp.next_step_id,
                    "activity": activity_id,
                },
            ).first()
            if nxt is None:
                raise PlanRejected("next_step_id 未登记")

        if cp.candidate_manifest_id is not None:
            cand = db.execute(
                text(
                    """SELECT 1 FROM candidate_manifests
                    WHERE project_id=:project AND id=:id"""
                ),
                {"project": activity["project_id"], "id": cp.candidate_manifest_id},
            ).first()
            if cand is None:
                raise PlanRejected("candidate_manifest 未持久化")

        digest = _checkpoint_content_digest(cp)
        existing = (
            db.execute(
                text("SELECT * FROM checkpoints WHERE content_digest=:digest"),
                {"digest": digest},
            )
            .mappings()
            .first()
        )
        if existing:
            return _from_row(existing)

        row = (
            db.execute(
                text(
                    """INSERT INTO checkpoints(
                      id,project_id,activity_id,attempt_id,kind,context_digest,
                      completed_step_ids,next_step_id,candidate_manifest_id,
                      workspace_manifest_digest,session_ref,artifact_ids,effect_ids,
                      content_digest)
                    VALUES(
                      :id,:project,:activity,:attempt,:kind,:context,
                      :completed,:next,:candidate,
                      :workspace,:session,:artifacts,:effects,
                      :digest)
                    RETURNING *"""
                ),
                {
                    "id": uuid4(),
                    "project": activity["project_id"],
                    "activity": activity_id,
                    "attempt": attempt["id"],
                    "kind": cp.kind,
                    "context": cp.context_digest,
                    "completed": list(cp.completed_step_ids),
                    "next": cp.next_step_id,
                    "candidate": cp.candidate_manifest_id,
                    "workspace": cp.workspace_manifest_digest,
                    "session": cp.session_ref,
                    "artifacts": list(cp.artifact_ids),
                    "effects": list(cp.effect_ids),
                    "digest": digest,
                },
            )
            .mappings()
            .one()
        )
        # Task 指针：仅 EXECUTE 且任务存在时更新 latest_checkpoint_id。
        if activity["task_id"] is not None:
            db.execute(
                text(
                    """UPDATE tasks SET latest_checkpoint_id=:cp, updated_at=clock_timestamp()
                    WHERE id=:task AND project_id=:project"""
                ),
                {
                    "cp": row["id"],
                    "task": activity["task_id"],
                    "project": activity["project_id"],
                },
            )
        return _from_row(row)


def list_checkpoints_for_activity(
    engine: Engine,
    activity_id: UUID,
    subject: str,
    project_ids: list[str],
    limit: int,
    after: UUID | None,
) -> list[CheckpointResource]:
    from .policies import ConfigurationVersions

    with engine.connect() as db:
        activity = (
            db.execute(
                text("SELECT project_id FROM activities WHERE id=:id"),
                {"id": activity_id},
            )
            .mappings()
            .first()
        )
        if activity is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, activity["project_id"], subject, project_ids)
        rows = db.execute(
            text(
                """SELECT * FROM checkpoints WHERE activity_id=:activity
            AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
              SELECT created_at,id FROM checkpoints WHERE id=CAST(:after AS uuid)
                AND activity_id=:activity))
            ORDER BY created_at,id LIMIT :limit"""
            ),
            {"activity": activity_id, "after": after, "limit": limit},
        ).mappings()
        return [_from_row(row) for row in rows]
