"""PROBE_MODEL：探测命令、context、ModelInvocation 与 outcome。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from evidence_ledger.content import encode
from sqlalchemy import Engine, text

from ..domain.cloud_endpoint import (
    CloudModeDenied,
    assert_chat_endpoint_allowed,
    assert_provider_model_matches_profile,
    resolve_frozen_provider_ref,
)
from ..protocols.audits import AuditCandidateOutcome, AuditGoalReviewOutcome
from ..protocols.candidates import ExecuteSuccessOutcome
from ..protocols.effects import ReconcileSuccessOutcome
from ..protocols.finalization import FinalizeSuccessOutcome
from ..protocols.integrate import IntegrateSuccessOutcome
from ..protocols.memory import IndexMemorySuccessOutcome
from ..protocols.models import (
    ContextBindRequest,
    ContextBindResponse,
    ModelDispatchRequest,
    ModelInvocationCreate,
    ModelInvocationResource,
    ModelReceipt,
    ModelReceiptResponse,
    ProbeSuccessOutcome,
)
from ..protocols.plans import ActivityOutcomeRequest, PlanSuccessOutcome
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
from .claims import binding_digest_of
from .plan_inputs import assert_attempt_plan_input
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict

PROBE_RESOURCES = Resources(
    cpu_millicores=100,
    memory_bytes=268435456,
    disk_bytes=67108864,
    model_slots=1,
    browser_slots=0,
    exclusive_labels=[],
)


def _content_digest(object_type: str, content: dict) -> str:
    canonical = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": object_type,
                "content": content,
                "reference_bindings": [],
            }
        )
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _invocation_from_row(row) -> ModelInvocationResource:
    data = dict(row)
    return ModelInvocationResource.model_validate(
        {
            **data,
            "data_categories": list(data["data_categories"] or []),
            "exposed_tools": list(data.get("exposed_tools") or []),
        }
    )


def list_invocations(
    engine: Engine,
    subject: str,
    project_ids: list[str],
    *,
    project_id: UUID,
    goal_id: UUID | None,
    activity_id: UUID | None,
    limit: int,
    after: UUID | None,
) -> list[ModelInvocationResource]:
    """按项目 ACL 列出调用元数据；不返回模型原始输入。"""
    with engine.connect() as db:
        ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
        rows = db.execute(
            text(
                """SELECT * FROM model_invocations WHERE project_id=:project
            AND (CAST(:goal AS uuid) IS NULL OR goal_id=CAST(:goal AS uuid))
            AND (CAST(:activity AS uuid) IS NULL OR activity_id=CAST(:activity AS uuid))
            AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
              SELECT created_at,id FROM model_invocations WHERE id=CAST(:after AS uuid)
                AND project_id=:project))
            ORDER BY created_at,id LIMIT :limit"""
            ),
            {
                "project": project_id,
                "goal": goal_id,
                "activity": activity_id,
                "after": after,
                "limit": limit,
            },
        ).mappings()
        return [_invocation_from_row(row) for row in rows]


def start_probe(
    engine: Engine,
    profile_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
) -> CommandOperation:
    path = f"/api/v1/model-profiles/{profile_id}/probe"
    request_scope = json.dumps([str(profile_id), subject, "POST", path, key], separators=(",", ":"))
    request_digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    with engine.begin() as db:
        profile = (
            db.execute(
                text("SELECT * FROM model_profiles WHERE id=:id FOR UPDATE"),
                {"id": profile_id},
            )
            .mappings()
            .first()
        )
        if profile is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, profile["project_id"], subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": profile["project_id"]},
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
        policy = (
            db.execute(
                text("""SELECT id,content_digest FROM policies
                WHERE project_id=:project ORDER BY version DESC LIMIT 1"""),
                {"project": profile["project_id"]},
            )
            .mappings()
            .first()
        )
        if policy is None:
            raise PlanRejected("项目尚无策略，无法探测模型")
        scope_lock = json.dumps(
            ["budget", str(profile["project_id"]), str(profile["project_id"])],
            separators=(",", ":"),
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": scope_lock},
        )
        binding = ExecutionBinding(
            goal_contract_revision=None,
            goal_contract_digest=None,
            task_contract_revision=None,
            task_contract_digest=None,
            plan_revision=None,
            subject_digest=profile["content_digest"],
            policy_digest=policy["content_digest"],
            model_profile_digest=profile["content_digest"],
            skill_set_digest=None,
        )
        activity_id = uuid4()
        db.execute(
            text("""INSERT INTO activities(
              id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
              binding,verification_assignments,status,state_revision,depends_on_activity_ids,
              retry_count,resources)
            VALUES(
              :id,:project,NULL,NULL,:project,'PROBE_MODEL','MODEL_PROFILE',:profile,
              CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,CAST(:resources AS jsonb))"""),
            {
                "id": activity_id,
                "project": profile["project_id"],
                "profile": profile_id,
                "binding": binding.model_dump_json(),
                "resources": PROBE_RESOURCES.model_dump_json(),
            },
        )
        command_id = uuid4()
        result = CommandResult(
            profile_id=profile_id,
            activity_id=activity_id,
            evidence_ids=[],
            capability_status="UNVERIFIED",
        )
        command_row = (
            db.execute(
                text("""INSERT INTO command_operations(
                  id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                VALUES(
                  :id,:project,NULL,'PROBE_MODEL','RUNNING',:digest,CAST(:result AS jsonb),NULL,:subject)
                RETURNING *"""),
                {
                    "id": command_id,
                    "project": profile["project_id"],
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
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'MODEL_PROBE_STARTED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": profile["project_id"],
                "payload": command.model_dump_json(),
            },
        )
        return command


def create_context_bundle(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    lease_activity_id: UUID,
    attempt_id: UUID,
    fencing_epoch: str,
    content: dict,
    *,
    candidate_verified: bool = False,
) -> tuple[UUID, str]:
    if lease_activity_id != activity_id:
        raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
    digest = _content_digest("ContextBundle", content)
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
        attempt = (
            db.execute(
                text("""SELECT * FROM activity_attempts
                WHERE id=:id AND activity_id=:activity FOR UPDATE"""),
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
        if attempt["lease_expires_at"] <= datetime.now(UTC):
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能创建上下文")
        activity = (
            db.execute(text("SELECT * FROM activities WHERE id=:id"), {"id": activity_id})
            .mappings()
            .one()
        )
        trust = db.execute(
            text(
                """SELECT status FROM project_trust_states
                WHERE project_id=:id FOR SHARE"""
            ),
            {"id": activity["project_id"]},
        ).scalar_one()
        if trust != "OPEN":
            raise TrustBlocked()
        if activity["kind"] == "PLAN" and activity["goal_id"] is not None:
            selected = assert_attempt_plan_input(db, activity, attempt, content=content)
            if selected is not None and not candidate_verified:
                raise PlanRejected("PLAN_INPUT_CONTEXT_UNVERIFIED: 须经对象仓校验后编译候选")
        # Temporal/宿主重试：同一 attempt 已编译则幂等返回（UNIQUE attempt_id）
        existing = (
            db.execute(
                text(
                    """SELECT id, content_digest FROM context_bundles
                    WHERE attempt_id=:attempt FOR SHARE"""
                ),
                {"attempt": attempt_id},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["content_digest"] != digest:
                raise LeaseRejected(
                    "INVALID_STATE",
                    "该 attempt 已有不同 digest 的 ContextBundle",
                )
            return existing["id"], digest
        bundle_id = uuid4()
        inserted = (
            db.execute(
                text("""INSERT INTO context_bundles(
                  id,project_id,activity_id,attempt_id,content_digest,content)
                VALUES(:id,:project,:activity,:attempt,:digest,CAST(:content AS jsonb))
                ON CONFLICT (attempt_id) DO NOTHING
                RETURNING id, content_digest"""),
                {
                    "id": bundle_id,
                    "project": activity["project_id"],
                    "activity": activity_id,
                    "attempt": attempt_id,
                    "digest": digest,
                    "content": json.dumps(content),
                },
            )
            .mappings()
            .first()
        )
        if inserted is not None:
            return inserted["id"], digest
        raced = (
            db.execute(
                text(
                    """SELECT id, content_digest FROM context_bundles
                    WHERE attempt_id=:attempt"""
                ),
                {"attempt": attempt_id},
            )
            .mappings()
            .one()
        )
        if raced["content_digest"] != digest:
            raise LeaseRejected(
                "INVALID_STATE",
                "该 attempt 已有不同 digest 的 ContextBundle",
            )
        return raced["id"], digest


def bind_context(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ContextBindRequest,
) -> ContextBindResponse:
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
        attempt = (
            db.execute(
                text("""SELECT * FROM activity_attempts
                WHERE id=:id AND activity_id=:activity FOR UPDATE"""),
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
        if attempt["lease_expires_at"] <= datetime.now(UTC):
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能绑定上下文")
        live = binding_digest_of(activity["binding"])
        if live != body.binding_digest:
            raise PlanRejected("binding_digest 与当前活动绑定不一致")
        bundle = (
            db.execute(
                text("""SELECT * FROM context_bundles
                WHERE id=:id AND activity_id=:activity AND attempt_id=:attempt"""),
                {
                    "id": body.context_bundle_id,
                    "activity": activity_id,
                    "attempt": body.lease.attempt_id,
                },
            )
            .mappings()
            .first()
        )
        if bundle is None:
            raise ScopeNotFound()
        if activity["kind"] == "PLAN" and activity["goal_id"] is not None:
            assert_attempt_plan_input(db, activity, attempt, content=bundle["content"])
        if attempt["context_digest"] is not None:
            if attempt["context_digest"] != bundle["content_digest"]:
                raise PlanRejected("context 已绑定且不可覆盖")
            return ContextBindResponse(
                context_digest=attempt["context_digest"],
                binding_digest=live,
            )
        db.execute(
            text("""UPDATE activity_attempts SET context_digest=:digest, updated_at=clock_timestamp()
              WHERE id=:id AND context_digest IS NULL"""),
            {"id": attempt["id"], "digest": bundle["content_digest"]},
        )
        return ContextBindResponse(
            context_digest=bundle["content_digest"],
            binding_digest=live,
        )


def _bound_model_profile_row(db, activity):
    """按 Activity 解析冻结 ModelProfile 行；缺则 None。"""
    profile_id = None
    if activity["kind"] == "PROBE_MODEL":
        profile_id = activity["target_id"]
    elif activity.get("goal_id") is not None:
        goal = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id"),
                {"id": activity["goal_id"]},
            )
            .mappings()
            .first()
        )
        if goal and goal.get("contract"):
            raw = goal["contract"].get("model_profile_id")
            if raw:
                profile_id = UUID(str(raw))
    if profile_id is None:
        return None
    return (
        db.execute(
            text("SELECT * FROM model_profiles WHERE id=:id FOR SHARE"),
            {"id": profile_id},
        )
        .mappings()
        .first()
    )


def _profile_config(profile) -> dict:
    config = profile["config"] if isinstance(profile["config"], dict) else {}
    return config


def assert_invocation_frozen_to_profile(
    db,
    activity,
    *,
    provider_ref: str,
    model_id: str,
) -> dict:
    """create / 幂等重放 / dispatch 共用：落库或派发前核对冻结 Profile。"""
    profile = _bound_model_profile_row(db, activity)
    if profile is None:
        raise CloudModeDenied("无法解析绑定 ModelProfile")
    config = _profile_config(profile)
    assert_provider_model_matches_profile(
        provider_ref=provider_ref,
        model_id=model_id,
        local_provider_ref=str(config.get("local_provider_ref") or ""),
        cloud_provider_refs=list(config.get("cloud_provider_refs") or []),
        profile_model_id=str(config.get("model_id") or ""),
    )
    return config


def create_invocation(
    engine: Engine,
    subject: str,
    body: ModelInvocationCreate,
) -> ModelInvocationResource:
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
                {"id": body.lease.activity_id},
            )
            .mappings()
            .first()
        )
        if activity is None:
            raise ScopeNotFound()
        verify_kinds = ("AUDIT", "VALIDATE_SKILL", "FINALIZE")
        if activity["kind"] not in verify_kinds and activity["kind"] not in (
            "PROBE_MODEL",
            "PLAN",
            "EXECUTE",
        ):
            raise PlanRejected("当前仅允许 PLAN/EXECUTE/PROBE_MODEL 登记模型调用")
        exposed_tools = [str(t).strip() for t in (body.exposed_tools or []) if str(t).strip()]
        if activity["kind"] == "PLAN" and exposed_tools:
            raise PlanRejected("PLAN 禁止暴露工具：exposed_tools 必须为空")
        attempt = (
            db.execute(
                text("""SELECT * FROM activity_attempts
                WHERE id=:id AND activity_id=:activity FOR UPDATE"""),
                {"id": body.lease.attempt_id, "activity": body.lease.activity_id},
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
        if attempt["lease_expires_at"] <= datetime.now(UTC):
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能登记模型调用")
        obligation = None
        if activity["kind"] in verify_kinds:
            from .obligations import resolve_open_obligation_for_prepare

            obligation = resolve_open_obligation_for_prepare(db, activity, attempt["id"])
        if attempt["context_digest"] != body.context_digest:
            raise PlanRejected("context_digest 与 attempt 绑定不一致")
        if activity["kind"] == "PLAN" and activity["goal_id"] is not None:
            bundle = (
                db.execute(
                    text("""SELECT content FROM context_bundles WHERE attempt_id=:attempt
                  AND content_digest=:digest"""),
                    {"attempt": attempt["id"], "digest": attempt["context_digest"]},
                )
                .mappings()
                .first()
            )
            if bundle is None:
                raise PlanRejected("PLAN_INPUT_CONTEXT_MISSING: 模型调用缺少固定上下文")
            assert_attempt_plan_input(db, activity, attempt, content=bundle["content"])
        existing = (
            db.execute(
                text("""SELECT * FROM model_invocations
                WHERE activity_id=:activity AND invocation_seq=:seq"""),
                {"activity": body.lease.activity_id, "seq": body.invocation_seq},
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["input_digest"] != body.input_digest:
                raise PlanRejected("MODEL_INPUT_CONFLICT")
            try:
                # 幂等重放也不得放行历史错绑的 provider/model
                assert_invocation_frozen_to_profile(
                    db,
                    activity,
                    provider_ref=str(existing["provider_ref"] or ""),
                    model_id=str(existing["model_id"] or ""),
                )
            except CloudModeDenied as exc:
                raise PlanRejected(exc.message) from exc
            if obligation is not None:
                from .obligations import link_invocation

                link_invocation(db, obligation["id"], existing["id"])
            return _invocation_from_row(existing)
        # doc/05 §3.11：Trust BLOCKED 拒新模型调用登记；幂等重放不受影响
        trust = db.execute(
            text(
                """SELECT status FROM project_trust_states
                WHERE project_id=:id FOR SHARE"""
            ),
            {"id": activity["project_id"]},
        ).scalar_one()
        if trust != "OPEN":
            raise TrustBlocked()
        if activity["goal_id"] is not None:
            from .stops import assert_goal_allows_new_inference

            assert_goal_allows_new_inference(db, activity["goal_id"])
        profile = _bound_model_profile_row(db, activity)
        if profile is None:
            raise PlanRejected("无法解析绑定 ModelProfile")
        config = _profile_config(profile)
        cloud_mode = str(config.get("cloud_mode") or "DENY")
        local_ref = str(config.get("local_provider_ref") or "")
        cloud_refs = list(config.get("cloud_provider_refs") or [])
        try:
            provider_ref = resolve_frozen_provider_ref(
                body.provider_ref,
                cloud_mode=cloud_mode,
                local_provider_ref=local_ref,
                cloud_provider_refs=cloud_refs,
            )
            assert_invocation_frozen_to_profile(
                db,
                activity,
                provider_ref=provider_ref,
                model_id=body.model_id,
            )
        except CloudModeDenied as exc:
            raise PlanRejected(exc.message) from exc
        binding_digest = binding_digest_of(activity["binding"])
        payload_content = {
            "activity_id": str(body.lease.activity_id),
            "invocation_seq": body.invocation_seq,
            "binding_digest": binding_digest,
            "context_digest": body.context_digest,
            "input_digest": body.input_digest,
            "provider_ref": provider_ref,
            "model_id": body.model_id,
            "max_output_tokens": body.max_output_tokens,
            "max_cost_usd": body.max_cost_usd,
            "data_categories": list(body.data_categories),
        }
        payload_digest = _content_digest("ModelInvocationInput", payload_content)
        reservation_id = uuid4()
        # 本地 DENY 云：直接 AUTHORIZED，不经审批。
        status = "AUTHORIZED"
        row = (
            db.execute(
                text("""INSERT INTO model_invocations(
                  id,project_id,goal_id,activity_id,producer_attempt_id,invocation_seq,
                  payload_digest,binding_digest,context_digest,input_digest,provider_ref,model_id,
                  max_output_tokens,max_cost_usd,data_categories,exposed_tools,status,state_revision,
                  reservation_id,usage_status)
                VALUES(
                  :id,:project,:goal,:activity,:attempt,:seq,
                  :payload,:binding,:context,:input,:provider,:model,
                  :max_out,:cost,:cats,:tools,:status,1,:reservation,'UNKNOWN')
                RETURNING *"""),
                {
                    "id": uuid4(),
                    "project": activity["project_id"],
                    "goal": activity["goal_id"],
                    "activity": body.lease.activity_id,
                    "attempt": body.lease.attempt_id,
                    "seq": body.invocation_seq,
                    "payload": payload_digest,
                    "binding": binding_digest,
                    "context": body.context_digest,
                    "input": body.input_digest,
                    "provider": provider_ref,
                    "model": body.model_id,
                    "max_out": body.max_output_tokens,
                    "cost": body.max_cost_usd,
                    "cats": body.data_categories,
                    "tools": exposed_tools,
                    "status": status,
                    "reservation": reservation_id,
                },
            )
            .mappings()
            .one()
        )
        if obligation is not None:
            from .obligations import link_invocation

            link_invocation(db, obligation["id"], row["id"])
        # BudgetUsage 预留：登记时占位；终态回执再释放（UNKNOWN 保留）
        if activity["goal_id"] is not None:
            from .budget_clock import adjust_goal_budget_reservations

            usage = adjust_goal_budget_reservations(
                db,
                activity["goal_id"],
                reserved_tokens_delta=int(body.max_output_tokens or 0),
                reserved_cost_usd_delta=body.max_cost_usd,
            )
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": activity["goal_id"]},
            ).scalar_one()
            if goal_status == "FAILED":
                raise PlanRejected(
                    "BUDGET_EXHAUSTED: 模型预留后预算耗尽（Goal FAILED，本登记回滚）"
                )
            _ = usage
        return _invocation_from_row(row)


def mark_dispatched(
    engine: Engine,
    subject: str,
    invocation_id: UUID,
    body: ModelDispatchRequest,
    *,
    endpoint_base: str,
    deployment_cloud_mode: str = "DENY",
) -> ModelInvocationResource:
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
        row = (
            db.execute(
                text("SELECT * FROM model_invocations WHERE id=:id FOR UPDATE"),
                {"id": invocation_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        if row["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        if body.lease.activity_id != row["activity_id"]:
            raise LeaseRejected("INVALID_REQUEST", "租约与调用活动不一致")
        attempt = (
            db.execute(
                text("SELECT * FROM activity_attempts WHERE id=:id"),
                {"id": body.lease.attempt_id},
            )
            .mappings()
            .first()
        )
        if attempt is None or attempt["worker_id"] != worker["id"]:
            raise WorkerForbidden()
        if str(attempt["fencing_epoch"]) != body.lease.fencing_epoch:
            raise LeaseRejected("FENCING_REJECTED", "fencing epoch 不匹配")
        if row["status"] in ("DISPATCHED", "SUCCEEDED", "FAILED", "UNKNOWN"):
            if row["status"] == "DISPATCHED":
                return _invocation_from_row(row)
            raise PlanRejected("调用已结束，不能再次 dispatch")
        if row["status"] != "AUTHORIZED":
            raise PlanRejected("调用未授权，不能 dispatch")
        activity = (
            db.execute(
                text("SELECT * FROM activities WHERE id=:id"),
                {"id": row["activity_id"]},
            )
            .mappings()
            .first()
        )
        if activity is None:
            raise ScopeNotFound()
        if activity["kind"] == "PLAN" and activity["goal_id"] is not None:
            if (
                row["producer_attempt_id"] != attempt["id"]
                or attempt["activity_id"] != activity["id"]
            ):
                raise LeaseRejected("FENCING_REJECTED", "模型调用与 attempt 不匹配")
            if attempt["status"] != "ACTIVE" or attempt["lease_expires_at"] <= datetime.now(UTC):
                raise LeaseRejected("LEASE_EXPIRED", "PLAN attempt 已失租")
            bundle = (
                db.execute(
                    text("""SELECT content FROM context_bundles WHERE attempt_id=:attempt
                  AND content_digest=:digest"""),
                    {"attempt": attempt["id"], "digest": attempt["context_digest"]},
                )
                .mappings()
                .first()
            )
            if bundle is None:
                raise PlanRejected("PLAN_INPUT_CONTEXT_MISSING: 模型派发缺少固定上下文")
            assert_attempt_plan_input(db, activity, attempt, content=bundle["content"])
        # doc/05 §3.11：BLOCKED 拒模型 dispatch；已 DISPATCHED 幂等回放不受影响
        trust = db.execute(
            text(
                """SELECT status FROM project_trust_states
                WHERE project_id=:id FOR SHARE"""
            ),
            {"id": activity["project_id"]},
        ).scalar_one()
        if trust != "OPEN":
            raise TrustBlocked()
        if activity.get("goal_id") is not None:
            from .stops import assert_goal_allows_new_inference

            # 未 DISPATCHED 的外呼等同新推理；已 DISPATCHED 幂等在上方返回
            assert_goal_allows_new_inference(db, activity["goal_id"])
        profile = _bound_model_profile_row(db, activity)
        if profile is None:
            raise CloudModeDenied("无法解析绑定 ModelProfile 的 cloud_mode")
        config = _profile_config(profile)
        refs = list(config.get("cloud_provider_refs") or [])
        # 历史错绑行不得进入 DISPATCHED
        assert_invocation_frozen_to_profile(
            db,
            activity,
            provider_ref=str(row["provider_ref"] or ""),
            model_id=str(row["model_id"] or ""),
        )
        # Runner 自持 chat（官方 Loop）时 Control 不外呼；不得用部署 local_qwen_base 闸门拦账本迁移
        if not body.runner_owned_completion:
            assert_chat_endpoint_allowed(
                base_url=endpoint_base,
                provider_ref=str(row["provider_ref"] or ""),
                cloud_mode=str(config.get("cloud_mode") or "DENY"),
                cloud_provider_refs=refs,
                deployment_cloud_mode=deployment_cloud_mode,
                approval_id=str(row["approval_id"]) if row.get("approval_id") else None,
            )
        updated = (
            db.execute(
                text("""UPDATE model_invocations SET status='DISPATCHED',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id RETURNING *"""),
                {"id": invocation_id},
            )
            .mappings()
            .one()
        )
        return _invocation_from_row(updated)


def apply_receipt(
    engine: Engine,
    subject: str,
    body: ModelReceipt,
) -> ModelReceiptResponse:
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
        existing = (
            db.execute(
                text("SELECT * FROM model_receipts WHERE receipt_id=:id"),
                {"id": body.receipt_id},
            )
            .mappings()
            .first()
        )
        if existing:
            return ModelReceiptResponse(disposition="DUPLICATE")
        invocation = (
            db.execute(
                text("SELECT * FROM model_invocations WHERE id=:id FOR UPDATE"),
                {"id": body.invocation_id},
            )
            .mappings()
            .first()
        )
        if invocation is None:
            raise ScopeNotFound()
        attempt = (
            db.execute(
                text("SELECT * FROM activity_attempts WHERE id=:id"),
                {"id": body.producer_attempt_id},
            )
            .mappings()
            .first()
        )
        if attempt is None or attempt["worker_id"] != worker["id"]:
            raise WorkerForbidden()
        if invocation["status"] not in ("DISPATCHED", "UNKNOWN"):
            raise PlanRejected("仅 DISPATCHED/UNKNOWN 可受理回执")
        if body.producer_attempt_id != invocation["producer_attempt_id"]:
            return ModelReceiptResponse(disposition="PENDING_RECONCILIATION")
        final = {
            "SUCCEEDED": "SUCCEEDED",
            "FAILED": "FAILED",
            "UNKNOWN": "UNKNOWN",
        }[body.observed_result]
        db.execute(
            text("""INSERT INTO model_receipts(
              receipt_id,invocation_id,project_id,producer_attempt_id,observed_result,
              usage_status,input_tokens,output_tokens,cost_usd,result_artifact_id,observed_at)
            VALUES(
              :rid,:iid,:project,:attempt,:result,:usage,:in_tok,:out_tok,:cost,:artifact,:at)"""),
            {
                "rid": body.receipt_id,
                "iid": body.invocation_id,
                "project": invocation["project_id"],
                "attempt": body.producer_attempt_id,
                "result": body.observed_result,
                "usage": body.usage_status,
                "in_tok": body.input_tokens,
                "out_tok": body.output_tokens,
                "cost": body.cost_usd,
                "artifact": body.result_artifact_id,
                "at": body.observed_at,
            },
        )
        db.execute(
            text("""UPDATE model_invocations SET status=:status, usage_status=:usage,
              result_artifact_id=:artifact, state_revision=state_revision+1,
              updated_at=clock_timestamp() WHERE id=:id"""),
            {
                "id": body.invocation_id,
                "status": final,
                "usage": body.usage_status,
                "artifact": body.result_artifact_id,
            },
        )
        # 终态 SUCCEEDED/FAILED：释放登记时预留（UNKNOWN 保留预留，doc/01）
        if final in ("SUCCEEDED", "FAILED") and invocation.get("goal_id") is not None:
            from .budget_clock import adjust_goal_budget_reservations

            max_tok = int(invocation.get("max_output_tokens") or 0)
            max_cost = invocation.get("max_cost_usd") or "0"
            adjust_goal_budget_reservations(
                db,
                invocation["goal_id"],
                reserved_tokens_delta=-max_tok,
                reserved_cost_usd_delta=(
                    str(-Decimal(str(max_cost))) if Decimal(str(max_cost)) != 0 else None
                ),
            )
        # BudgetUsage：SUCCEEDED + CONFIRMED 用量计入 Goal 账（token/cost）
        if (
            final == "SUCCEEDED"
            and body.usage_status == "CONFIRMED"
            and invocation.get("goal_id") is not None
        ):
            from .budget_clock import record_goal_budget_meters

            in_tok = int(body.input_tokens or 0)
            out_tok = int(body.output_tokens or 0)
            delta = in_tok + out_tok
            cost_raw = body.cost_usd
            gpu_raw = body.gpu_seconds
            if (
                delta > 0
                or (cost_raw is not None and str(cost_raw) not in ("", "0"))
                or (gpu_raw is not None and str(gpu_raw) not in ("", "0"))
            ):
                kwargs: dict = {}
                if delta > 0:
                    kwargs["consumed_tokens_delta"] = delta
                if cost_raw is not None and str(cost_raw) not in ("", "0"):
                    kwargs["consumed_cost_usd_delta"] = cost_raw
                if gpu_raw is not None and str(gpu_raw) not in ("", "0"):
                    kwargs["gpu_seconds_delta"] = gpu_raw
                if kwargs:
                    record_goal_budget_meters(
                        db,
                        invocation["goal_id"],
                        **kwargs,
                    )
        return ModelReceiptResponse(disposition="APPLIED")


def submit_probe_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if not isinstance(body.outcome, ProbeSuccessOutcome):
        raise PlanRejected("PROBE_MODEL outcome 形状无效")
    outcome = body.outcome
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
        if activity["kind"] != "PROBE_MODEL":
            raise PlanRejected("非 PROBE_MODEL 活动")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if activity["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        attempt = (
            db.execute(
                text("""SELECT * FROM activity_attempts
                WHERE id=:id AND activity_id=:activity FOR UPDATE"""),
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
        if outcome.profile_id != activity["target_id"]:
            raise PlanRejected("outcome.profile_id 与活动 target 不一致")
        db.execute(
            text("""UPDATE model_profiles
              SET capability_status=:status, probe_evidence_ids=:evidence,
                  updated_at=clock_timestamp()
              WHERE id=:id"""),
            {
                "id": outcome.profile_id,
                "status": outcome.capability_status,
                "evidence": outcome.evidence_ids,
            },
        )
        db.execute(
            text("""UPDATE activity_attempts SET status='COMPLETED', finished_at=:now,
              updated_at=clock_timestamp() WHERE id=:id"""),
            {"id": attempt["id"], "now": now},
        )
        db.execute(
            text("""UPDATE resource_reservations SET status='RELEASED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""),
            {"id": attempt["id"]},
        )
        db.execute(
            text("""UPDATE budget_reservations SET status='RELEASED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""),
            {"id": attempt["id"]},
        )
        updated = (
            db.execute(
                text("""UPDATE activities SET status='SUCCEEDED',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id RETURNING *"""),
                {"id": activity_id},
            )
            .mappings()
            .one()
        )
        result = CommandResult(
            profile_id=outcome.profile_id,
            activity_id=activity_id,
            evidence_ids=outcome.evidence_ids,
            capability_status=outcome.capability_status,
        )
        db.execute(
            text("""UPDATE command_operations SET status='SUCCEEDED',
              result=CAST(:result AS jsonb), updated_at=clock_timestamp()
              WHERE project_id=:project AND kind='PROBE_MODEL'
                AND result->>'activity_id'=:activity AND status='RUNNING'"""),
            {
                "project": activity["project_id"],
                "activity": str(activity_id),
                "result": result.model_dump_json(),
            },
        )
        return _activity_from_row(updated)


def submit_activity_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if isinstance(body.outcome, PlanSuccessOutcome):
        from .plans import submit_plan_outcome

        result = submit_plan_outcome(engine, subject, activity_id, body)
    elif isinstance(body.outcome, ExecuteSuccessOutcome):
        from .candidates import submit_execute_outcome

        result = submit_execute_outcome(engine, subject, activity_id, body)
    elif isinstance(body.outcome, (AuditCandidateOutcome, AuditGoalReviewOutcome)):
        from .audits import submit_audit_outcome

        result = submit_audit_outcome(engine, subject, activity_id, body)
    elif isinstance(body.outcome, FinalizeSuccessOutcome):
        from .finalization import submit_finalize_outcome

        result = submit_finalize_outcome(engine, subject, activity_id, body)
    elif isinstance(body.outcome, IntegrateSuccessOutcome):
        from .integrate import submit_integrate_outcome

        result = submit_integrate_outcome(engine, subject, activity_id, body)
    elif isinstance(body.outcome, ReconcileSuccessOutcome):
        from .reconcile import submit_reconcile_outcome

        result = submit_reconcile_outcome(engine, subject, activity_id, body)
    elif isinstance(body.outcome, ValidateSkillSuccessOutcome):
        from .skill_validate import submit_validate_outcome

        result = submit_validate_outcome(engine, subject, activity_id, body)
    elif isinstance(body.outcome, IndexMemorySuccessOutcome):
        from .memory_index import submit_index_outcome

        result = submit_index_outcome(engine, subject, activity_id, body)
    else:
        result = submit_probe_outcome(engine, subject, activity_id, body)

    if result.goal_id is not None:
        from .control_commands import maybe_complete_pause_or_cancel

        with engine.begin() as db:
            maybe_complete_pause_or_cancel(db, result.goal_id)
            if result.task_id is not None:
                from .task_commands import maybe_complete_task_cancel

                maybe_complete_task_cancel(db, result.task_id)
    elif result.task_id is not None:
        from .task_commands import maybe_complete_task_cancel

        with engine.begin() as db:
            maybe_complete_task_cancel(db, result.task_id)
    return result
