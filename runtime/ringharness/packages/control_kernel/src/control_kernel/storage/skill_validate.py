"""VALIDATE_SKILL：创建无 Goal 验证活动；outcome 写入 SkillValidationRecord 并更新 Skill 状态。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from evidence_ledger.content import encode
from sqlalchemy import Engine, text

from ..protocols.plans import ActivityOutcomeRequest
from ..protocols.runtime import (
    ActivityResource,
    CommandOperation,
    CommandResult,
    ExecutionBinding,
    LeaseRejected,
    PlanRejected,
    Resources,
    StateRevisionConflict,
    WorkerForbidden,
)
from ..protocols.skills import ValidateSkillSuccessOutcome
from .activities import _activity_from_row, _command_from_row
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict
from .verification_runs import require_matching_verifier_runs

VALIDATE_RESOURCES = Resources(
    cpu_millicores=100,
    memory_bytes=268435456,
    disk_bytes=67108864,
    model_slots=0,
    browser_slots=0,
    exclusive_labels=[],
)


def _content_digest(content: dict) -> str:
    canonical = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": "SkillValidationRecord",
                "content": content,
                "reference_bindings": [],
            },
            separators=(",", ":"),
        )
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def start_validate(
    engine: Engine,
    version_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
) -> CommandOperation:
    path = f"/api/v1/skills/{version_id}/validate"
    request_scope = json.dumps(
        [str(version_id), subject, "POST", path, key], separators=(",", ":")
    )
    request_digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    with engine.begin() as db:
        skill = (
            db.execute(
                text("SELECT * FROM skill_versions WHERE id=:id FOR UPDATE"),
                {"id": version_id},
            )
            .mappings()
            .first()
        )
        if skill is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, skill["project_id"], subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": skill["project_id"]},
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
        if skill["status"] != "CANDIDATE":
            raise PlanRejected(f"技能状态为 {skill['status']}，不可启动验证")
        # 已有进行中的 VALIDATE_SKILL
        open_act = db.execute(
            text(
                """SELECT 1 FROM activities
                WHERE kind='VALIDATE_SKILL' AND target_type='SKILL_VERSION' AND target_id=:id
                  AND status IN ('READY','RUNNING','WAITING','RECOVERING')
                LIMIT 1"""
            ),
            {"id": version_id},
        ).first()
        if open_act is not None:
            raise PlanRejected("已有进行中的技能验证活动")

        # VALIDATE_SKILL 前再核 required_tools（对照现行策略 allowlist）
        from ..domain.tool_capability_manifest import (
            ToolCapabilityRejected,
            assert_skill_required_tools_admissible,
        )

        skill_config = skill["config"]
        if isinstance(skill_config, str):
            skill_config = json.loads(skill_config)
        required_tools = list(skill_config.get("required_tools") or [])
        policy = (
            db.execute(
                text(
                    """SELECT content_digest, config FROM policies
                    WHERE project_id=:project ORDER BY version DESC, created_at DESC LIMIT 1"""
                ),
                {"project": skill["project_id"]},
            )
            .mappings()
            .first()
        )
        if policy is None:
            raise PlanRejected("项目尚无策略")
        try:
            assert_skill_required_tools_admissible(
                required_tools,
                policy_allowed_tools=list((policy["config"] or {}).get("allowed_tools") or []),
            )
        except ToolCapabilityRejected as err:
            raise PlanRejected(f"{err.code}: {err.message}") from err

        profile = (
            db.execute(
                text(
                    """SELECT id, content_digest, config FROM verification_profiles
                    WHERE id=:id AND project_id=:project"""
                ),
                {
                    "id": skill_config["verification_profile_id"],
                    "project": skill["project_id"],
                },
            )
            .mappings()
            .first()
        )
        if profile is None or profile["config"].get("target_scope") != "SKILL":
            raise PlanRejected("技能验证配置无效")
        layers = list(profile["config"].get("required_layers") or ["MECHANICAL"])
        layer = layers[0]
        prior_rounds = db.execute(
            text(
                """SELECT COALESCE(MAX((content->>'audit_round')::int), 0)
                FROM skill_validation_records
                WHERE subject_skill_version_id=:id"""
            ),
            {"id": version_id},
        ).scalar_one()
        audit_round = int(prior_rounds) + 1
        binding = ExecutionBinding(
            goal_contract_revision=None,
            goal_contract_digest=None,
            task_contract_revision=None,
            task_contract_digest=None,
            plan_revision=None,
            subject_digest=skill["content_digest"],
            policy_digest=policy["content_digest"],
            model_profile_digest=None,
            skill_set_digest=None,
        )
        assignments = [
            {
                "subject_type": "SKILL_VERSION",
                "subject_id": str(version_id),
                "subject_digest": skill["content_digest"],
                "verification_profile_id": str(profile["id"]),
                "profile_digest": profile["content_digest"],
                "layer": layer,
                "audit_round": audit_round,
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
                  :id,:project,NULL,NULL,:project,'VALIDATE_SKILL','SKILL_VERSION',:skill,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'READY',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": activity_id,
                "project": skill["project_id"],
                "skill": version_id,
                "binding": binding.model_dump_json(),
                "assignments": json.dumps(assignments),
                "resources": VALIDATE_RESOURCES.model_dump_json(),
            },
        )
        db.execute(
            text(
                """UPDATE skill_versions SET status='VALIDATING', updated_at=clock_timestamp()
                WHERE id=:id AND status='CANDIDATE'"""
            ),
            {"id": version_id},
        )
        command_id = uuid4()
        result = CommandResult(version_id=version_id, activity_id=activity_id)
        command_row = (
            db.execute(
                text(
                    """INSERT INTO command_operations(
                      id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                    VALUES(
                      :id,:project,NULL,'VALIDATE_SKILL','RUNNING',:digest,
                      CAST(:result AS jsonb),NULL,:subject)
                    RETURNING *"""
                ),
                {
                    "id": command_id,
                    "project": skill["project_id"],
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
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'SKILL_VALIDATE_STARTED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": skill["project_id"],
                "payload": command.model_dump_json(),
            },
        )
        return command


def submit_validate_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if not isinstance(body.outcome, ValidateSkillSuccessOutcome):
        raise PlanRejected("VALIDATE_SKILL 需要 ValidateSkillSuccessOutcome")
    outcome = body.outcome
    validation = outcome.validation
    if body.lease.activity_id != activity_id:
        raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
    with engine.begin() as db:
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
        activity = (
            db.execute(
                text("SELECT * FROM activities WHERE id=:id FOR UPDATE"),
                {"id": activity_id},
            )
            .mappings()
            .first()
        )
        if activity is None:
            raise ScopeNotFound()
        if activity["kind"] != "VALIDATE_SKILL":
            raise PlanRejected("非 VALIDATE_SKILL 活动")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if activity["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        if activity["target_id"] != outcome.version_id:
            raise PlanRejected("outcome.version_id 与活动 target 不一致")
        attempt = (
            db.execute(
                text(
                    """SELECT * FROM activity_attempts
                    WHERE id=:id AND activity_id=:activity FOR UPDATE"""
                ),
                {"id": body.lease.attempt_id, "activity": activity_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise ScopeNotFound()
        if attempt["worker_id"] != worker["id"]:
            raise WorkerForbidden()
        if str(attempt["fencing_epoch"]) != body.lease.fencing_epoch:
            raise LeaseRejected("FENCING_REJECTED", "fencing epoch 不匹配")
        if attempt["status"] != "ACTIVE":
            raise LeaseRejected("INVALID_STATE", "attempt 已非 ACTIVE")
        now = datetime.now(UTC)
        expires = attempt["lease_expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能复活")

        skill = (
            db.execute(
                text("SELECT * FROM skill_versions WHERE id=:id FOR UPDATE"),
                {"id": outcome.version_id},
            )
            .mappings()
            .first()
        )
        if skill is None:
            raise ScopeNotFound()
        if skill["status"] != "VALIDATING":
            raise PlanRejected("技能未处于 VALIDATING")
        if validation.subject_digest != skill["content_digest"]:
            raise PlanRejected("subject_digest 与技能内容不一致")

        # PASS 前再次核工具 fixture（防中途改策略/目录后仍放行）
        from ..domain.tool_capability_manifest import (
            ToolCapabilityRejected,
            assert_skill_required_tools_admissible,
        )

        skill_cfg = skill["config"]
        if isinstance(skill_cfg, str):
            skill_cfg = json.loads(skill_cfg)
        if validation.verdict == "PASS":
            try:
                assert_skill_required_tools_admissible(
                    list(skill_cfg.get("required_tools") or [])
                )
            except ToolCapabilityRejected as err:
                raise PlanRejected(f"{err.code}: {err.message}") from err

        assignments = activity["verification_assignments"] or []
        if not assignments:
            raise PlanRejected("活动缺少 verification_assignments")
        assignment = assignments[0]
        if str(validation.verification_profile_id) != str(
            assignment["verification_profile_id"]
        ):
            raise PlanRejected("verification_profile 与分配不一致")
        if int(validation.audit_round) != int(assignment.get("audit_round", 0)):
            raise PlanRejected("audit_round 与分配不一致")

        # 严格：每个 verifier_run_id 须存在且与本活动 lease/assignment/subject 对齐。
        require_matching_verifier_runs(
            db,
            run_ids=list(validation.verifier_run_ids),
            producer_activity_id=activity_id,
            producer_attempt_id=attempt["id"],
            subject_type="SKILL_VERSION",
            subject_id=outcome.version_id,
            verification_profile_id=validation.verification_profile_id,
            layer=str(assignment.get("layer")),
            audit_round=int(validation.audit_round),
        )

        item_verdicts = {c.criterion_id: c.verdict for c in validation.criterion_results}
        if validation.verdict == "PASS" and any(v != "PASS" for v in item_verdicts.values()):
            raise PlanRejected("VALIDATION_VERDICT_MISMATCH")
        if validation.verdict == "FAIL" and all(v == "PASS" for v in item_verdicts.values()):
            raise PlanRejected("VALIDATION_VERDICT_MISMATCH")
        required = {
            c["id"]
            for c in (skill["config"].get("acceptance") or [])
            if c.get("required")
        }
        if validation.verdict == "PASS" and not required.issubset(item_verdicts.keys()):
            raise PlanRejected("缺少必需 criterion 结果")
        if validation.verdict == "PASS" and any(
            item_verdicts.get(cid) != "PASS" for cid in required
        ):
            raise PlanRejected("必需 criterion 未全部 PASS")

        content = {
            "project_id": str(skill["project_id"]),
            "producer_activity_id": str(activity_id),
            "producer_attempt_id": str(attempt["id"]),
            "subject_skill_version_id": str(outcome.version_id),
            "subject_digest": validation.subject_digest,
            "verification_profile_id": str(validation.verification_profile_id),
            "audit_round": validation.audit_round,
            "verifier_run_ids": [str(i) for i in validation.verifier_run_ids],
            "verdict": validation.verdict,
            "criterion_results": [
                c.model_dump(mode="json") for c in validation.criterion_results
            ],
            "evidence_ids": [str(i) for i in validation.evidence_ids],
            "reason": validation.reason,
        }
        digest = _content_digest(content)
        record_id = uuid4()
        db.execute(
            text(
                """INSERT INTO skill_validation_records(
                  id,project_id,subject_skill_version_id,subject_digest,verification_profile_id,
                  content_digest,content,verdict)
                VALUES(
                  :id,:project,:version,:subject,:profile,:digest,CAST(:content AS jsonb),:verdict)"""
            ),
            {
                "id": record_id,
                "project": skill["project_id"],
                "version": outcome.version_id,
                "subject": validation.subject_digest,
                "profile": validation.verification_profile_id,
                "digest": digest,
                "content": json.dumps(content),
                "verdict": validation.verdict,
            },
        )
        if validation.verdict == "FAIL":
            new_status = "REJECTED"
        else:
            # PASS / INSUFFICIENT → 回 CANDIDATE，待 activate
            new_status = "CANDIDATE"
        db.execute(
            text(
                """UPDATE skill_versions SET status=:status, updated_at=clock_timestamp()
                WHERE id=:id AND status='VALIDATING'"""
            ),
            {"id": outcome.version_id, "status": new_status},
        )
        db.execute(
            text(
                """UPDATE activity_attempts SET status='COMPLETED', finished_at=:now,
                  updated_at=clock_timestamp() WHERE id=:id"""
            ),
            {"id": attempt["id"], "now": now},
        )
        updated = (
            db.execute(
                text(
                    """UPDATE activities SET status='SUCCEEDED',
                      state_revision=state_revision+1, current_attempt_id=NULL,
                      updated_at=clock_timestamp()
                      WHERE id=:id RETURNING *"""
                ),
                {"id": activity_id},
            )
            .mappings()
            .one()
        )
        cmd_result = CommandResult(
            version_id=outcome.version_id,
            activity_id=activity_id,
            audit_id=record_id,
            verdict=validation.verdict,
        )
        db.execute(
            text(
                """UPDATE command_operations
                  SET status='SUCCEEDED', result=CAST(:result AS jsonb),
                      updated_at=clock_timestamp()
                  WHERE kind='VALIDATE_SKILL' AND status='RUNNING'
                    AND result->>'activity_id'=:activity"""
            ),
            {
                "result": cmd_result.model_dump_json(),
                "activity": str(activity_id),
            },
        )
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'SKILL_VALIDATED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": skill["project_id"],
                "payload": json.dumps(
                    {
                        "version_id": str(outcome.version_id),
                        "audit_id": str(record_id),
                        "verdict": validation.verdict,
                        "skill_status": new_status,
                    },
                    separators=(",", ":"),
                ),
            },
        )
        return _activity_from_row(updated)
