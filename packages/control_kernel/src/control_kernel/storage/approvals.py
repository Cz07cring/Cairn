"""审批查询、决定与撤销；prepare 绑定逻辑在 effects 中调用。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text

from ..protocols.approvals import (
    ApprovalDecision,
    ApprovalResource,
    ApprovalScope,
)
from ..protocols.runtime import ControlRequest, StateRevisionConflict
from .policies import ConfigurationVersions, ScopeNotFound


class ApprovalRejected(Exception):
    """审批状态不允许当前操作。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _from_row(row) -> ApprovalResource:
    data = dict(row)
    scope = data["scope"] if isinstance(data["scope"], dict) else dict(data["scope"] or {})
    return ApprovalResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "state_revision": data["state_revision"],
            "subject": {"type": data["subject_type"], "id": data["subject_id"]},
            "payload_digest": data["payload_digest"],
            "policy_version": data["policy_version"],
            "scope": scope,
            "max_cost_usd": data["max_cost_usd"],
            "expires_at": data["expires_at"],
            "status": data["status"],
            "consumed_subject_id": data["consumed_subject_id"],
            "decision_reason": data["decision_reason"],
        }
    )


def create_effect_approval(
    db: Connection,
    *,
    project_id: UUID,
    goal_id: UUID | None,
    effect_id: UUID,
    payload_digest: str,
    policy_version: int,
    tool_ref: str,
    max_cost_usd: str = "0",
    ttl: timedelta | None = None,
) -> UUID:
    """在同一事务内创建 PENDING 审批并返回 id；调用方负责写回 effect.approval_id。"""
    approval_id = uuid4()
    expires = datetime.now(UTC) + (ttl or timedelta(hours=24))
    scope = ApprovalScope(tools=[tool_ref]).model_dump(mode="json")
    db.execute(
        text(
            """INSERT INTO approvals(
              id,project_id,goal_id,state_revision,subject_type,subject_id,
              payload_digest,policy_version,scope,max_cost_usd,expires_at,status)
            VALUES(
              :id,:project,:goal,1,'EFFECT',:subject,
              :digest,:policy,CAST(:scope AS jsonb),:cost,:expires,'PENDING')"""
        ),
        {
            "id": approval_id,
            "project": project_id,
            "goal": goal_id,
            "subject": effect_id,
            "digest": payload_digest,
            "policy": policy_version,
            "scope": json.dumps(scope, separators=(",", ":")),
            "cost": max_cost_usd,
            "expires": expires,
        },
    )
    return approval_id


def require_approved_for_dispatch(db: Connection, approval_id: UUID, subject_id: UUID) -> None:
    """dispatch 消费前校验；已消费同一 subject 可安全重传。"""
    row = (
        db.execute(
            text("SELECT * FROM approvals WHERE id=:id FOR UPDATE"),
            {"id": approval_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApprovalRejected("审批不存在")
    now = datetime.now(UTC)
    if row["status"] == "APPROVED" and row["expires_at"] <= now:
        db.execute(
            text(
                """UPDATE approvals SET status='EXPIRED', state_revision=state_revision+1,
                  updated_at=clock_timestamp() WHERE id=:id AND status='APPROVED'"""
            ),
            {"id": approval_id},
        )
        raise ApprovalRejected("审批已过期")
    if row["status"] != "APPROVED":
        raise ApprovalRejected("审批未通过或已失效")
    if row["expires_at"] <= now:
        raise ApprovalRejected("审批已过期")
    if row["consumed_subject_id"] is not None and row["consumed_subject_id"] != subject_id:
        raise ApprovalRejected("审批已绑定其他主体")
    if row["consumed_subject_id"] is None:
        db.execute(
            text(
                """UPDATE approvals SET consumed_subject_id=:subject,
                  updated_at=clock_timestamp() WHERE id=:id AND consumed_subject_id IS NULL"""
            ),
            {"id": approval_id, "subject": subject_id},
        )


class Approvals:
    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self, approval_id: UUID, subject: str, project_ids: list[str]) -> ApprovalResource:
        with self.engine.connect() as db:
            row = (
                db.execute(text("SELECT * FROM approvals WHERE id=:id"), {"id": approval_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            return _from_row(row)

    def list_for_project(
        self,
        project_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        after: UUID | None,
        status: str | None,
    ) -> list[ApprovalResource]:
        with self.engine.connect() as db:
            ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
            rows = db.execute(
                text(
                    """SELECT * FROM approvals WHERE project_id=:project
                AND (CAST(:status AS text) IS NULL OR status=CAST(:status AS text))
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                  SELECT created_at,id FROM approvals WHERE id=CAST(:after AS uuid)
                    AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""
                ),
                {
                    "project": project_id,
                    "status": status,
                    "after": after,
                    "limit": limit,
                },
            ).mappings()
            return [_from_row(row) for row in rows]

    def decide(
        self,
        approval_id: UUID,
        subject: str,
        project_ids: list[str],
        body: ApprovalDecision,
    ) -> ApprovalResource:
        with self.engine.begin() as db:
            row = (
                db.execute(
                    text("SELECT * FROM approvals WHERE id=:id FOR UPDATE"),
                    {"id": approval_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            if row["state_revision"] != body.expected_state_revision:
                raise StateRevisionConflict()
            if row["status"] != "PENDING":
                raise ApprovalRejected("仅 PENDING 可裁决")
            if datetime.now(UTC) >= row["expires_at"]:
                db.execute(
                    text(
                        """UPDATE approvals SET status='EXPIRED',
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id"""
                    ),
                    {"id": approval_id},
                )
                raise ApprovalRejected("审批已过期")
            if (
                body.subject.type != row["subject_type"]
                or body.subject.id != row["subject_id"]
                or body.payload_digest != row["payload_digest"]
            ):
                raise ApprovalRejected("裁决绑定与原审批不一致")
            new_status = "APPROVED" if body.decision == "APPROVE" else "DENIED"
            updated = (
                db.execute(
                    text(
                        """UPDATE approvals SET status=:status, decision_reason=:reason,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {
                        "id": approval_id,
                        "status": new_status,
                        "reason": body.reason,
                    },
                )
                .mappings()
                .one()
            )
            if new_status == "APPROVED" and row["subject_type"] == "EFFECT":
                db.execute(
                    text(
                        """UPDATE effect_intents SET status='AUTHORIZED',
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id AND approval_id=:approval AND status='PREPARED'"""
                    ),
                    {"id": row["subject_id"], "approval": approval_id},
                )
            return _from_row(updated)

    def revoke(
        self,
        approval_id: UUID,
        subject: str,
        project_ids: list[str],
        body: ControlRequest,
    ) -> ApprovalResource:
        with self.engine.begin() as db:
            row = (
                db.execute(
                    text("SELECT * FROM approvals WHERE id=:id FOR UPDATE"),
                    {"id": approval_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            if row["state_revision"] != body.expected_state_revision:
                raise StateRevisionConflict()
            if row["status"] not in ("PENDING", "APPROVED"):
                raise ApprovalRejected("当前状态不可撤销")
            # 已 DISPATCHED 的 effect 不抹除；仅标记审批撤销，后续 dispatch 拒绝。
            updated = (
                db.execute(
                    text(
                        """UPDATE approvals SET status='REVOKED', decision_reason=:reason,
                          state_revision=state_revision+1, updated_at=clock_timestamp()
                          WHERE id=:id RETURNING *"""
                    ),
                    {"id": approval_id, "reason": body.reason},
                )
                .mappings()
                .one()
            )
            return _from_row(updated)
