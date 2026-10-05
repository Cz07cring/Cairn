"""证据导出命令：仅排队 READY EXPORT_EVIDENCE Activity，不生成导出字节或 DSSE。"""

from __future__ import annotations

import hashlib
import json
import os
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.finalization import EvidenceExportRequest
from ..protocols.runtime import (
    CommandOperation,
    CommandResult,
    ExecutionBinding,
    PlanRejected,
    Resources,
)
from .activities import _command_from_row
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict

EXPORT_RESOURCES = Resources(
    cpu_millicores=100,
    memory_bytes=268435456,
    disk_bytes=67108864,
    model_slots=0,
    browser_slots=0,
    exclusive_labels=[],
)


class SigningUnavailable(Exception):
    """离线可验证导出缺少签名/信任根配置；失败关闭，禁止静默降级。"""


def start_evidence_export(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
    body: EvidenceExportRequest,
) -> CommandOperation:
    path = f"/api/v1/goals/{goal_id}/evidence-exports"
    request_scope = json.dumps(
        [str(goal_id), subject, "POST", path, key], separators=(",", ":")
    )
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
        goal = (
            db.execute(
                text("SELECT * FROM goals WHERE id=:id FOR UPDATE"),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if goal is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, goal["project_id"], subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": goal["project_id"]},
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
            return CommandOperation.model_validate(old["result"])
        if trust != "OPEN":
            raise TrustBlocked()
        if goal["release_manifest_id"] is None:
            raise PlanRejected("目标尚无发布清单，无法导出证据")
        if goal["release_manifest_id"] != body.release_manifest_id:
            raise PlanRejected("release_manifest_id 不属于该目标")
        # 仅终态可导出；FAILED/CANCELLED 须已有 ReleaseManifest（与 claim 门禁一致）
        if goal["status"] not in ("DONE", "CANCELLED", "FAILED"):
            raise PlanRejected(f"目标状态为 {goal['status']}，不可导出证据")
        release = (
            db.execute(
                text(
                    """SELECT * FROM release_manifests
                    WHERE id=:id AND goal_id=:goal AND project_id=:project"""
                ),
                {
                    "id": body.release_manifest_id,
                    "goal": goal_id,
                    "project": goal["project_id"],
                },
            )
            .mappings()
            .first()
        )
        if release is None:
            raise PlanRejected("发布清单不存在或不属于该目标")
        if body.trust_mode == "OFFLINE_VERIFIABLE":
            invalidated = db.execute(
                text(
                    """SELECT 1 FROM release_validity_records
                    WHERE release_manifest_id=:id LIMIT 1"""
                ),
                {"id": body.release_manifest_id},
            ).first()
            if invalidated is not None:
                raise PlanRejected(
                    "发布证据已失效（ReleaseValidity INVALIDATED），拒绝 OFFLINE_VERIFIABLE 导出"
                )
            attestation = (os.environ.get("RING_ATTESTATION_PROVIDER_REF") or "").strip()
            trust_bundle = (os.environ.get("RING_TRUST_BUNDLE_REF") or "").strip()
            if not attestation or not trust_bundle:
                raise SigningUnavailable()

        contract = goal["contract"]
        if isinstance(contract, str):
            contract = json.loads(contract)
        policy = db.execute(
            text("SELECT content_digest FROM policies WHERE id=:id"),
            {"id": contract["policy_id"]},
        ).scalar_one()
        model = db.execute(
            text("SELECT content_digest FROM model_profiles WHERE id=:id"),
            {"id": contract["model_profile_id"]},
        ).scalar_one()
        skill = db.execute(
            text("SELECT content_digest FROM skill_sets WHERE id=:id"),
            {"id": contract["skill_set_id"]},
        ).scalar_one()
        # binding 必须与 claim._live_binding(EXPORT_EVIDENCE) 一致
        binding = ExecutionBinding(
            goal_contract_revision=goal["contract_revision"],
            goal_contract_digest=goal["contract_digest"],
            task_contract_revision=None,
            task_contract_digest=None,
            plan_revision=goal["plan_revision"],
            subject_digest=release["content_digest"],
            policy_digest=policy,
            model_profile_digest=model,
            skill_set_digest=skill,
        )
        # trust_mode 写入 verification_assignments，供 claim/lease 侧读取（非验收指派）
        export_assignments = [
            {
                "trust_mode": body.trust_mode,
                "release_manifest_id": str(body.release_manifest_id),
            }
        ]

        activity_id = uuid4()
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,NULL,:budget,'EXPORT_EVIDENCE','RELEASE_EXPORT',:release,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'READY',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": activity_id,
                "project": goal["project_id"],
                "goal": goal_id,
                "budget": goal["project_id"],
                "release": body.release_manifest_id,
                "binding": binding.model_dump_json(),
                "assignments": json.dumps(export_assignments),
                "resources": EXPORT_RESOURCES.model_dump_json(),
            },
        )
        # 诚实切片：仅排队，不捏造 artifact_id，命令保持 RUNNING
        result = CommandResult(
            activity_id=activity_id,
            trust_mode=body.trust_mode,
        )
        command_id = uuid4()
        command_row = (
            db.execute(
                text(
                    """INSERT INTO command_operations(
                      id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                    VALUES(
                      :id,:project,:goal,'EXPORT_EVIDENCE','RUNNING',:digest,
                      CAST(:result AS jsonb),NULL,:subject)
                    RETURNING *"""
                ),
                {
                    "id": command_id,
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "digest": request_digest,
                    "result": result.model_dump_json(),
                    "subject": subject,
                },
            )
            .mappings()
            .one()
        )
        command = _command_from_row(command_row)
        db.execute(
            text(
                "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
            ),
            {
                "scope": request_scope,
                "digest": request_digest,
                "result": command.model_dump_json(),
            },
        )
        from .events import append_goal_event

        append_goal_event(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            event_type="COMMAND_CHANGED",
            entity_id=command_id,
            entity_state_revision=None,
            resource_type="COMMAND",
        )
        return command
