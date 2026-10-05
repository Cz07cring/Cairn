"""EXECUTE 步骤登记与 Effect prepare；PLAN 一律 ROLE_TOOL_FORBIDDEN。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..domain.tool_capability_manifest import (
    TOOL_REGISTRY,
    ToolCapabilityRejected,
    admit_tool_prepare,
    assert_run_tests_suite_for_activity,
    parse_tool_input_artifact,
    require_manifest,
)
from ..protocols.effects import (
    BrokerDispatchableEffect,
    EffectDispatchRequest,
    EffectPrepareRequest,
    EffectResource,
    ReceiptAccepted,
    StepCreate,
    StepResource,
    TrustedReceipt,
)
from ..protocols.runtime import (
    LeaseIdentity,
    LeaseRejected,
    PlanRejected,
    StateRevisionConflict,
    WorkerForbidden,
)
from .policies import ScopeNotFound, TrustBlocked

# TOOL_REGISTRY 权威见 domain.tool_capability_manifest；此处再导出供历史导入。


class RoleToolForbidden(Exception):
    """PLAN 等角色禁止工具路径。"""


def _step_from_row(row) -> StepResource:
    data = dict(row)
    return StepResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "activity_id": data["activity_id"],
            "logical_step_id": data["logical_step_id"],
            "predecessor_step_id": data["predecessor_step_id"],
            "purpose": data["purpose"],
            "tool_ref": data["tool_ref"],
            "intent_revision": data["intent_revision"],
            "effect_id": data["effect_id"],
        }
    )


def _effect_from_row(row) -> EffectResource:
    data = dict(row)
    return EffectResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "activity_id": data["activity_id"],
            "logical_step_id": data["logical_step_id"],
            "intent_revision": data["intent_revision"],
            "payload_digest": data["payload_digest"],
            "tool_ref": data["tool_ref"],
            "replay_class": data["replay_class"],
            "scope": data["scope"],
            "write_epoch": data["write_epoch"],
            "status": data["status"],
            "state_revision": data["state_revision"],
            "approval_id": data["approval_id"],
            "reservation_id": data["reservation_id"],
            "external_ref": data["external_ref"],
            "evidence_ids": list(data["evidence_ids"] or []),
            "input_artifact_id": data["input_artifact_id"],
        }
    )


def _assert_seal_requires_green_tests_after_write(db, activity_id: UUID) -> None:
    """若本活动已有成功 write_file，seal 前必须有其后的绿测回执。

    无 write_file 时不拦（纯封存既有树的路径仍合法）。≠ Goal DONE。
    """
    last_write = (
        db.execute(
            text(
                """SELECT id, created_at FROM effect_intents
                WHERE activity_id=:aid
                  AND tool_ref='write_file'
                  AND status='SUCCEEDED'
                  AND scope='ENGINEERING'
                ORDER BY created_at DESC, id DESC
                LIMIT 1"""
            ),
            {"aid": activity_id},
        )
        .mappings()
        .first()
    )
    if last_write is None:
        return
    green = db.execute(
        text(
            """SELECT 1 FROM effect_intents ei
            INNER JOIN effect_receipts er ON er.effect_id = ei.id
              AND er.disposition = 'APPLIED'
              AND er.observed_outcome = 'SUCCEEDED'
              AND er.exit_code = 0
            WHERE ei.activity_id=:aid
              AND ei.tool_ref='run_tests'
              AND ei.status='SUCCEEDED'
              AND ei.scope='ENGINEERING'
              AND (ei.created_at, ei.id) > (:w_at, :w_id)
            LIMIT 1"""
        ),
        {
            "aid": activity_id,
            "w_at": last_write["created_at"],
            "w_id": last_write["id"],
        },
    ).first()
    if green is None:
        raise PlanRejected(
            "SEAL_REQUIRES_GREEN_TESTS: write_file 之后须有 exit_code=0 的 run_tests"
        )


def _require_owner_attempt(db, subject: str, activity_id: UUID, lease) -> tuple:
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
    if lease.activity_id != activity_id:
        raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
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
            {"id": lease.attempt_id, "activity": activity_id},
        )
        .mappings()
        .first()
    )
    if attempt is None:
        raise ScopeNotFound()
    if attempt["worker_id"] != worker["id"]:
        raise WorkerForbidden()
    if str(attempt["fencing_epoch"]) != lease.fencing_epoch:
        raise LeaseRejected("FENCING_REJECTED", "fencing epoch 不匹配")
    if attempt["status"] != "ACTIVE":
        raise LeaseRejected("INVALID_STATE", "attempt 已非 ACTIVE")
    return activity, attempt


def create_step(engine: Engine, subject: str, activity_id: UUID, body: StepCreate) -> StepResource:
    with engine.begin() as db:
        # Issue #22：admission 先于 activity/attempt 行锁
        goal_id = db.execute(
            text("SELECT goal_id FROM activities WHERE id=:id"),
            {"id": activity_id},
        ).scalar()
        if goal_id is not None:
            from .goals import acquire_goal_admission_lock

            acquire_goal_admission_lock(db, goal_id)
        activity, attempt = _require_owner_attempt(db, subject, activity_id, body.lease)
        if activity["kind"] == "PLAN":
            raise RoleToolForbidden()
        verify_step_kinds = ("AUDIT", "VALIDATE_SKILL", "FINALIZE")
        verify_allowed_tools = frozenset({"run_tests", "read_file"})
        if activity["kind"] == "EXECUTE":
            if activity["status"] != "RUNNING":
                raise PlanRejected("活动未处于 RUNNING")
            if activity["goal_id"] is not None:
                from .stops import assert_goal_allows_new_engineering_writes

                assert_goal_allows_new_engineering_writes(db, activity["goal_id"])
        elif activity["kind"] in verify_step_kinds:
            # 验证活动：只读工具步骤；不走工程写入门（VERIFYING 仍可跑测）
            if activity["status"] != "RUNNING":
                raise PlanRejected("活动未处于 RUNNING")
            if body.tool_ref not in verify_allowed_tools:
                raise PlanRejected("验证活动仅允许 run_tests/read_file")
        else:
            raise PlanRejected("当前活动 kind 不允许登记步骤")
        if body.tool_ref not in TOOL_REGISTRY:
            raise PlanRejected("工具未在受信目录登记")
        try:
            require_manifest(body.tool_ref)
        except ToolCapabilityRejected as err:
            raise PlanRejected(err.message) from err

        if body.predecessor_step_id is None:
            existing = (
                db.execute(
                    text("""SELECT * FROM activity_steps
                    WHERE activity_id=:activity AND predecessor_step_id IS NULL"""),
                    {"activity": activity_id},
                )
                .mappings()
                .first()
            )
        else:
            pred = (
                db.execute(
                    text("""SELECT * FROM activity_steps
                    WHERE id=:id AND activity_id=:activity"""),
                    {"id": body.predecessor_step_id, "activity": activity_id},
                )
                .mappings()
                .first()
            )
            if pred is None:
                raise ScopeNotFound()
            if pred["effect_id"] is None:
                raise PlanRejected("前驱步骤尚无确定 effect，不能登记后继")
            effect = (
                db.execute(
                    text("SELECT status FROM effect_intents WHERE id=:id"),
                    {"id": pred["effect_id"]},
                )
                .mappings()
                .one()
            )
            if effect["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                raise PlanRejected("前驱 effect 未到确定终态")
            existing = (
                db.execute(
                    text("""SELECT * FROM activity_steps
                    WHERE activity_id=:activity AND predecessor_step_id=:pred"""),
                    {"activity": activity_id, "pred": body.predecessor_step_id},
                )
                .mappings()
                .first()
            )

        if existing:
            if (
                existing["purpose"] != body.purpose
                or existing["tool_ref"] != body.tool_ref
            ):
                # 未绑定 effect 的占位步骤：允许模型换参/换工具覆盖，避免卡死链
                if existing["effect_id"] is None:
                    row = (
                        db.execute(
                            text(
                                """UPDATE activity_steps
                                SET purpose=:purpose, tool_ref=:tool,
                                    updated_at=clock_timestamp()
                                WHERE id=:id AND effect_id IS NULL
                                RETURNING *"""
                            ),
                            {
                                "id": existing["id"],
                                "purpose": body.purpose,
                                "tool": body.tool_ref,
                            },
                        )
                        .mappings()
                        .first()
                    )
                    if row is None:
                        raise PlanRejected("STEP_CONFLICT")
                    return _step_from_row(row)
                raise PlanRejected("STEP_CONFLICT")
            return _step_from_row(existing)

        # doc/05 §3.11：BLOCKED 拒新工程步骤；幂等重放不受影响
        trust = db.execute(
            text(
                """SELECT status FROM project_trust_states
                WHERE project_id=:id FOR SHARE"""
            ),
            {"id": activity["project_id"]},
        ).scalar_one()
        if trust != "OPEN":
            raise TrustBlocked()

        step_id = uuid4()
        row = (
            db.execute(
                text("""INSERT INTO activity_steps(
                  id,project_id,activity_id,attempt_id,logical_step_id,predecessor_step_id,
                  purpose,tool_ref,intent_revision)
                VALUES(
                  :id,:project,:activity,:attempt,:id,:pred,
                  :purpose,:tool,1)
                RETURNING *"""),
                {
                    "id": step_id,
                    "project": activity["project_id"],
                    "activity": activity_id,
                    "attempt": attempt["id"],
                    "pred": body.predecessor_step_id,
                    "purpose": body.purpose,
                    "tool": body.tool_ref,
                },
            )
            .mappings()
            .one()
        )
        return _step_from_row(row)


def prepare_effect(
    engine: Engine,
    subject: str,
    body: EffectPrepareRequest,
    *,
    read_artifact_bytes: Callable[[UUID, str], bytes] | None = None,
) -> EffectResource:
    with engine.begin() as db:
        # Issue #22：admission 先于 activity/attempt 行锁
        goal_id = db.execute(
            text("SELECT goal_id FROM activities WHERE id=:id"),
            {"id": body.lease.activity_id},
        ).scalar()
        if goal_id is not None:
            from .goals import acquire_goal_admission_lock

            acquire_goal_admission_lock(db, goal_id)
        activity, attempt = _require_owner_attempt(
            db, subject, body.lease.activity_id, body.lease
        )
        if activity["kind"] == "PLAN":
            raise RoleToolForbidden()
        verify_kinds = ("AUDIT", "VALIDATE_SKILL", "FINALIZE")
        obligation = None
        if activity["kind"] in verify_kinds:
            from .obligations import resolve_open_obligation_for_prepare

            # 验证类 prepare 必须关联已开立的 OPEN 义务
            obligation = resolve_open_obligation_for_prepare(
                db, activity, attempt["id"]
            )
        elif activity["kind"] != "EXECUTE":
            raise PlanRejected("当前仅允许 EXECUTE 准备 effect")
        if activity["kind"] == "EXECUTE" and activity["goal_id"] is not None:
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": activity["goal_id"]},
            ).scalar_one()
            # 控制排空/终态：连幂等续跑 prepare 也不准入；Stop 屏障只拦「新写入」（见下方）
            if goal_status in (
                "PAUSING",
                "PAUSED",
                "CANCELLING",
                "CANCELLED",
                "BLOCKED",
                "VERIFYING",
                "DONE",
                "FAILED",
            ):
                raise PlanRejected(
                    f"GOAL_ENGINEERING_CLOSED: Goal 处于 {goal_status}，禁止 ENGINEERING prepare"
                )
            sealed = db.execute(
                text("""SELECT 1 FROM finalization_barriers
                WHERE goal_id=:goal AND status IN ('DRAINING','SEALED','RELEASED')
                LIMIT 1"""),
                {"goal": activity["goal_id"]},
            ).first()
            if sealed is not None:
                raise PlanRejected("最终屏障已开启，禁止新的 ENGINEERING effect")
        if body.intent_revision != 1:
            raise PlanRejected("V1 intent_revision 必须为 1")
        if body.tool_ref not in TOOL_REGISTRY:
            raise PlanRejected("工具未在受信目录登记")

        step = (
            db.execute(
                text("""SELECT * FROM activity_steps
                WHERE activity_id=:activity AND logical_step_id=:step FOR UPDATE"""),
                {"activity": body.lease.activity_id, "step": body.logical_step_id},
            )
            .mappings()
            .first()
        )
        if step is None:
            raise ScopeNotFound()
        if step["tool_ref"] != body.tool_ref:
            raise PlanRejected("tool_ref 与步骤不一致")
        if step["effect_id"] is not None:
            existing = (
                db.execute(
                    text("SELECT * FROM effect_intents WHERE id=:id FOR UPDATE"),
                    {"id": step["effect_id"]},
                )
                .mappings()
                .one()
            )
            if existing["input_artifact_id"] != body.input_artifact_id:
                raise PlanRejected("EFFECT_CONFLICT")
            # AB02：重领后续跑原 PREPARED/AUTHORIZED——改绑 producer_attempt，不新建 effect
            if (
                existing["status"] in ("PREPARED", "AUTHORIZED")
                and existing["producer_attempt_id"] != attempt["id"]
            ):
                # doc/05 §3.11：BLOCKED 拒工程改绑（新执行者准入）
                trust = db.execute(
                    text(
                        """SELECT status FROM project_trust_states
                        WHERE project_id=:id FOR SHARE"""
                    ),
                    {"id": activity["project_id"]},
                ).scalar_one()
                if trust != "OPEN":
                    raise TrustBlocked()
                existing = (
                    db.execute(
                        text(
                            """UPDATE effect_intents
                              SET producer_attempt_id=:attempt,
                                  updated_at=clock_timestamp()
                              WHERE id=:id AND status IN ('PREPARED','AUTHORIZED')
                              RETURNING *"""
                        ),
                        {"attempt": attempt["id"], "id": existing["id"]},
                    )
                    .mappings()
                    .one()
                )
            if obligation is not None:
                from .obligations import link_effect

                link_effect(db, obligation["id"], existing["id"])
            return _effect_from_row(existing)

        # 新写入：未确认 Stop / 隔离资源时失败关闭（AB09）；幂等续跑已在上方返回
        # doc/05 §3.11：BLOCKED 拒新工具 prepare
        trust = db.execute(
            text(
                """SELECT status FROM project_trust_states
                WHERE project_id=:id FOR SHARE"""
            ),
            {"id": activity["project_id"]},
        ).scalar_one()
        if trust != "OPEN":
            raise TrustBlocked()
        if activity["kind"] == "EXECUTE" and activity["goal_id"] is not None:
            from .stops import assert_goal_allows_new_engineering_writes

            assert_goal_allows_new_engineering_writes(db, activity["goal_id"])

        artifact = (
            db.execute(
                text("""SELECT id, digest FROM artifacts
                WHERE id=:id AND project_id=:project"""),
                {"id": body.input_artifact_id, "project": activity["project_id"]},
            )
            .mappings()
            .first()
        )
        if artifact is None:
            raise ScopeNotFound()

        # 策略准入：使用 Goal 合同绑定的 policy，无 Goal 则取项目最新版本。
        if activity["goal_id"] is not None:
            goal_row = (
                db.execute(
                    text("SELECT contract FROM goals WHERE id=:id"),
                    {"id": activity["goal_id"]},
                )
                .mappings()
                .one()
            )
            contract = goal_row["contract"]
            if isinstance(contract, str):
                contract = json.loads(contract)
            policy_id = contract.get("policy_id")
            policy = (
                db.execute(
                    text("SELECT version, config FROM policies WHERE id=:id"),
                    {"id": policy_id},
                )
                .mappings()
                .first()
            )
        else:
            policy = (
                db.execute(
                    text("""SELECT version, config FROM policies
                    WHERE project_id=:project ORDER BY version DESC, created_at DESC LIMIT 1"""),
                    {"project": activity["project_id"]},
                )
                .mappings()
                .first()
            )
        if policy is None:
            raise PlanRejected("项目尚无策略")
        allowed = set(policy["config"].get("allowed_tools") or [])
        if body.tool_ref not in allowed:
            raise PlanRejected("策略不允许该工具")
        action_mode = None
        for action in policy["config"].get("external_actions") or []:
            if action.get("action") == body.tool_ref:
                action_mode = action.get("mode")
                break
        if action_mode == "DENY":
            raise PlanRejected("策略拒绝该外部动作")

        # 同活动已登记工具（含当前）→ 危险组合门
        sibling_tools = list(
            db.execute(
                text(
                    """SELECT DISTINCT tool_ref FROM activity_steps
                    WHERE activity_id=:activity"""
                ),
                {"activity": body.lease.activity_id},
            ).scalars()
        )

        input_bytes: bytes | None = None
        if read_artifact_bytes is not None:
            try:
                input_bytes = read_artifact_bytes(
                    activity["project_id"], artifact["digest"]
                )
            except FileNotFoundError as exc:
                raise ScopeNotFound() from exc
        else:
            # 失败关闭：prepare 必须能核验参数/path，禁止无对象仓绕过
            raise PlanRejected("对象仓未配置，无法核验工具输入工件")

        try:
            manifest = admit_tool_prepare(
                tool_ref=body.tool_ref,
                input_artifact_bytes=input_bytes,
                policy_allowed_tools=list(allowed),
                policy_allowed_paths=list(policy["config"].get("allowed_paths") or []),
                policy_protected_paths=list(
                    policy["config"].get("protected_paths") or []
                ),
                policy_network_allowlist=list(
                    policy["config"].get("network_allowlist") or []
                ),
                sibling_tool_refs=sibling_tools,
            )
        except ToolCapabilityRejected as err:
            raise PlanRejected(f"{err.code}: {err.message}") from err

        # write_file 成功后：须有 APPLIED 绿测（exit_code=0）才允许准备 seal_candidate
        if body.tool_ref == "seal_candidate" and activity["kind"] == "EXECUTE":
            _assert_seal_requires_green_tests_after_write(db, body.lease.activity_id)

        # run_tests：EXECUTE↔public / 验证活动↔auditor（三权）
        if body.tool_ref == "run_tests":
            _tool, _digest, run_params = parse_tool_input_artifact(input_bytes)
            try:
                assert_run_tests_suite_for_activity(str(activity["kind"]), run_params)
            except ToolCapabilityRejected as err:
                raise PlanRejected(f"{err.code}: {err.message}") from err

        # AUDIT/FINALIZE/VALIDATE_SKILL：只读验证工具，scope 强制 VERIFICATION
        effect_scope = manifest.scope
        if activity["kind"] in verify_kinds:
            if body.tool_ref not in ("run_tests", "read_file"):
                raise PlanRejected("验证活动仅允许 run_tests/read_file")
            if manifest.effect_class != "READ":
                raise PlanRejected("验证活动禁止非 READ 工具")
            effect_scope = "VERIFICATION"

        payload = {
            "activity_id": str(body.lease.activity_id),
            "logical_step_id": str(body.logical_step_id),
            "intent_revision": body.intent_revision,
            "tool_ref": body.tool_ref,
            "tool_schema_digest": manifest.schema_digest,
            "input_artifact_id": str(body.input_artifact_id),
        }
        digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        effect_id = uuid4()
        row = (
            db.execute(
                text("""INSERT INTO effect_intents(
                  id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                  payload_digest,tool_ref,replay_class,scope,write_epoch,status,state_revision,
                  evidence_ids,input_artifact_id,producer_attempt_id)
                VALUES(
                  :id,:project,:goal,:activity,:step,:rev,
                  :digest,:tool,:replay,:scope,NULL,'PREPARED',1,
                  '{}',:input,:attempt)
                RETURNING *"""),
                {
                    "id": effect_id,
                    "project": activity["project_id"],
                    "goal": activity["goal_id"],
                    "activity": body.lease.activity_id,
                    "step": body.logical_step_id,
                    "rev": body.intent_revision,
                    "digest": digest,
                    "tool": body.tool_ref,
                    "replay": manifest.replay_class,
                    "scope": effect_scope,
                    "input": body.input_artifact_id,
                    "attempt": attempt["id"],
                },
            )
            .mappings()
            .one()
        )
        if action_mode == "APPROVAL":
            from .approvals import create_effect_approval

            approval_id = create_effect_approval(
                db,
                project_id=activity["project_id"],
                goal_id=activity["goal_id"],
                effect_id=effect_id,
                payload_digest=digest,
                policy_version=policy["version"],
                tool_ref=body.tool_ref,
            )
            row = (
                db.execute(
                    text(
                        """UPDATE effect_intents SET approval_id=:approval,
                          updated_at=clock_timestamp() WHERE id=:id RETURNING *"""
                    ),
                    {"approval": approval_id, "id": effect_id},
                )
                .mappings()
                .one()
            )
        db.execute(
            text("""UPDATE activity_steps SET effect_id=:effect, updated_at=clock_timestamp()
              WHERE id=:id AND effect_id IS NULL"""),
            {"effect": effect_id, "id": step["id"]},
        )
        if obligation is not None:
            from .obligations import link_effect

            link_effect(db, obligation["id"], effect_id)
        return _effect_from_row(row)


def get_effect(engine: Engine, effect_id: UUID, project_ids: list[str]) -> EffectResource:
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM effect_intents WHERE id=:id"),
                {"id": effect_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        if project_ids and str(row["project_id"]) not in project_ids:
            raise ScopeNotFound()
        return _effect_from_row(row)


def list_dispatchable_effects_for_worker(
    engine: Engine, subject: str, *, limit: int = 32
) -> list[BrokerDispatchableEffect]:
    """列出本 worker ACTIVE 租约下可派发的 effect（PREPARED/AUTHORIZED）。

    不认领活动、不推进 Goal/Task；Kernel 仍为调度权威。
    """
    limit = max(1, min(limit, 200))
    with engine.connect() as db:
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
        rows = db.execute(
            text(
                """SELECT ei.*,
                      aa.id AS lease_attempt_id,
                      aa.fencing_epoch AS lease_fencing_epoch,
                      aa.lease_expires_at AS lease_expires_at
                FROM effect_intents ei
                INNER JOIN activity_attempts aa
                  ON aa.id = ei.producer_attempt_id
                 AND aa.activity_id = ei.activity_id
                 AND aa.status = 'ACTIVE'
                 AND aa.worker_id = :worker
                WHERE ei.status IN ('PREPARED', 'AUTHORIZED')
                ORDER BY ei.created_at, ei.id
                LIMIT :limit"""
            ),
            {"worker": worker["id"], "limit": limit},
        ).mappings()
        out: list[BrokerDispatchableEffect] = []
        for row in rows:
            effect = _effect_from_row(row)
            out.append(
                BrokerDispatchableEffect(
                    effect=effect,
                    lease=LeaseIdentity(
                        activity_id=effect.activity_id,
                        attempt_id=row["lease_attempt_id"],
                        fencing_epoch=str(row["lease_fencing_epoch"]),
                    ),
                    lease_expires_at=row["lease_expires_at"],
                )
            )
        return out


def list_effects(
    engine: Engine,
    subject: str,
    project_ids: list[str],
    *,
    project_id: UUID,
    goal_id: UUID | None,
    activity_id: UUID | None,
    status: str | None,
    limit: int,
    after: UUID | None,
) -> list[EffectResource]:
    with engine.connect() as db:
        from .policies import ConfigurationVersions

        ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
        rows = db.execute(
            text(
                """SELECT * FROM effect_intents WHERE project_id=:project
            AND (CAST(:goal AS uuid) IS NULL OR goal_id=CAST(:goal AS uuid))
            AND (CAST(:activity AS uuid) IS NULL OR activity_id=CAST(:activity AS uuid))
            AND (CAST(:status AS text) IS NULL OR status=CAST(:status AS text))
            AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
              SELECT created_at,id FROM effect_intents WHERE id=CAST(:after AS uuid)
                AND project_id=:project))
            ORDER BY created_at,id LIMIT :limit"""
            ),
            {
                "project": project_id,
                "goal": goal_id,
                "activity": activity_id,
                "status": status,
                "after": after,
                "limit": limit,
            },
        ).mappings()
        return [_effect_from_row(row) for row in rows]


def list_steps_for_activity(
    engine: Engine,
    activity_id: UUID,
    subject: str,
    project_ids: list[str],
    limit: int,
    after: UUID | None,
) -> list[StepResource]:
    with engine.connect() as db:
        from .policies import ConfigurationVersions

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
                """SELECT * FROM activity_steps WHERE activity_id=:activity
            AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
              SELECT created_at,id FROM activity_steps WHERE id=CAST(:after AS uuid)
                AND activity_id=:activity))
            ORDER BY created_at,id LIMIT :limit"""
            ),
            {"activity": activity_id, "after": after, "limit": limit},
        ).mappings()
        return [_step_from_row(row) for row in rows]


def dispatch_effect(
    engine: Engine, subject: str, effect_id: UUID, body: EffectDispatchRequest
) -> EffectResource:
    with engine.begin() as db:
        # Issue #22：admission 先于 activity/attempt 行锁
        goal_id = db.execute(
            text("SELECT goal_id FROM activities WHERE id=:id"),
            {"id": body.lease.activity_id},
        ).scalar()
        if goal_id is not None:
            from .goals import acquire_goal_admission_lock

            acquire_goal_admission_lock(db, goal_id)
        activity, _attempt = _require_owner_attempt(
            db, subject, body.lease.activity_id, body.lease
        )
        if activity["kind"] == "PLAN":
            raise RoleToolForbidden()
        row = (
            db.execute(
                text("SELECT * FROM effect_intents WHERE id=:id FOR UPDATE"),
                {"id": effect_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        if row["activity_id"] != body.lease.activity_id:
            raise LeaseRejected("INVALID_REQUEST", "effect 与租约活动不一致")
        if row["state_revision"] != body.effect_state_revision:
            raise StateRevisionConflict()
        if row["status"] == "DISPATCHED":
            return _effect_from_row(row)
        if row["status"] not in ("PREPARED", "AUTHORIZED"):
            raise PlanRejected("effect 状态不可 dispatch")
        # doc/05 §3.11：BLOCKED 拒工具 dispatch；已 DISPATCHED 幂等回放不受影响
        trust = db.execute(
            text(
                """SELECT status FROM project_trust_states
                WHERE project_id=:id FOR SHARE"""
            ),
            {"id": activity["project_id"]},
        ).scalar_one()
        if trust != "OPEN":
            raise TrustBlocked()
        if activity["kind"] == "EXECUTE" and activity["goal_id"] is not None:
            from .stops import assert_goal_allows_new_engineering_writes

            # 外部发出前再拦：未确认 Stop 时禁止 DISPATCHED（AB09 × AB02）
            assert_goal_allows_new_engineering_writes(db, activity["goal_id"])
        if row["approval_id"] is not None:
            from .approvals import ApprovalRejected, require_approved_for_dispatch

            try:
                require_approved_for_dispatch(db, row["approval_id"], row["id"])
            except ApprovalRejected as exc:
                raise PlanRejected(exc.message) from exc
            if row["status"] != "AUTHORIZED":
                raise PlanRejected("需审批通过后方可 dispatch")
        # READ_ONLY 工具：准入后置 DISPATCHED，由 ExecutionBroker 在事务外真实执行。
        updated = (
            db.execute(
                text("""UPDATE effect_intents
                  SET status='DISPATCHED', state_revision=state_revision+1,
                      updated_at=clock_timestamp()
                  WHERE id=:id AND status IN ('PREPARED','AUTHORIZED')
                  RETURNING *"""),
                {"id": effect_id},
            )
            .mappings()
            .one()
        )
        return _effect_from_row(updated)


def _require_attempt_holder(
    db,
    subject: str,
    attempt_id: UUID,
    *,
    fencing_epoch: str | None = None,
):
    """回执路径身份：subject 须为 attempt 持有者 worker。

    不要求 attempt ACTIVE——失租后的迟到回执须能进入 reconciliation，
    不得因 ACTIVE 门禁丢弃唯一真实结果。身份不符时抛 WorkerForbidden，
    调用方必须零副作用（不落 receipt、不改状态、不释放资源）。
    """
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
            text("SELECT * FROM activity_attempts WHERE id=:id FOR SHARE"),
            {"id": attempt_id},
        )
        .mappings()
        .first()
    )
    if attempt is None:
        raise ScopeNotFound()
    if attempt["worker_id"] != worker["id"]:
        raise WorkerForbidden()
    if fencing_epoch is not None and str(attempt["fencing_epoch"]) != fencing_epoch:
        raise LeaseRejected("FENCING_REJECTED", "fencing epoch 不匹配")
    return worker, attempt


def apply_effect_receipt(
    engine: Engine, subject: str, effect_id: UUID, body: TrustedReceipt
) -> tuple[ReceiptAccepted, EffectResource]:
    """写入 TrustedReceipt；迟到/参数矛盾可 PENDING_RECONCILIATION，不直接推进失租 attempt。

    权威持有者取自 effect.producer_attempt_id（不得信请求体自报 attempt）。
    身份不符零副作用；失租后原持有者仍可交回执（不要求 attempt ACTIVE）。
    """
    if body.effect_id != effect_id:
        raise LeaseRejected("INVALID_REQUEST", "回执与路径 effect 不一致")
    content = body.model_dump(mode="json")
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
        meta = (
            db.execute(
                text(
                    """SELECT e.id, a.goal_id
                    FROM effect_intents e
                    JOIN activities a ON a.id = e.activity_id
                    WHERE e.id=:id"""
                ),
                {"id": effect_id},
            )
            .mappings()
            .first()
        )
        if meta is None:
            raise ScopeNotFound()
        if meta["goal_id"] is not None:
            from .goals import acquire_goal_admission_lock

            acquire_goal_admission_lock(db, meta["goal_id"])
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": meta["goal_id"]},
            ).scalar()
            if goal_status in ("DONE", "FAILED", "CANCELLED"):
                old = (
                    db.execute(
                        text(
                            """SELECT * FROM effect_receipts
                            WHERE effect_id=:effect AND receipt_id=:receipt"""
                        ),
                        {"effect": effect_id, "receipt": body.receipt_id},
                    )
                    .mappings()
                    .first()
                )
                if old is not None:
                    if old["content_digest"] != digest:
                        raise PlanRejected("RECEIPT_CONFLICT")
                    effect = (
                        db.execute(
                            text("SELECT * FROM effect_intents WHERE id=:id"),
                            {"id": effect_id},
                        )
                        .mappings()
                        .one()
                    )
                    return (
                        ReceiptAccepted(
                            receipt_id=body.receipt_id, disposition=old["disposition"]
                        ),
                        _effect_from_row(effect),
                    )
                raise PlanRejected(
                    f"GOAL_ENGINEERING_CLOSED: Goal 处于 {goal_status}，拒绝新 effect 回执"
                )

        effect = (
            db.execute(
                text("SELECT * FROM effect_intents WHERE id=:id FOR UPDATE"),
                {"id": effect_id},
            )
            .mappings()
            .first()
        )
        if effect is None:
            raise ScopeNotFound()

        # 身份门禁先于任何写入：权威 owner = effect.producer_attempt_id（不得信请求体自报）
        if effect["producer_attempt_id"] is None:
            raise PlanRejected("EFFECT_PRODUCER_ATTEMPT_MISSING: effect 未登记 producer_attempt")
        _worker, attempt = _require_attempt_holder(
            db,
            subject,
            effect["producer_attempt_id"],
        )
        if attempt["activity_id"] != effect["activity_id"]:
            raise LeaseRejected("INVALID_REQUEST", "producer_attempt 与 effect 活动不一致")
        if body.producer_activity_id != effect["activity_id"]:
            # 活动不一致：正确 owner 仍可落 PENDING 供对账，不得推进业务态
            fields_match = False
        else:
            fields_match = (
                body.producer_attempt_id == effect["producer_attempt_id"]
                and str(attempt["fencing_epoch"]) == body.fencing_epoch
            )

        old = (
            db.execute(
                text("""SELECT * FROM effect_receipts
                WHERE effect_id=:effect AND receipt_id=:receipt"""),
                {"effect": effect_id, "receipt": body.receipt_id},
            )
            .mappings()
            .first()
        )
        if old:
            if old["content_digest"] != digest:
                raise PlanRejected("RECEIPT_CONFLICT")
            return (
                ReceiptAccepted(receipt_id=body.receipt_id, disposition=old["disposition"]),
                _effect_from_row(effect),
            )

        live_owner = (
            fields_match
            and attempt["status"] == "ACTIVE"
            and effect["status"] == "DISPATCHED"
        )
        if live_owner:
            disposition = "APPLIED"
            new_status = {
                "SUCCEEDED": "SUCCEEDED",
                "FAILED": "FAILED",
                "UNKNOWN": "UNKNOWN",
            }[body.observed_outcome]
            evidence = list(effect["evidence_ids"] or []) + list(body.result_artifact_ids)
            effect = (
                db.execute(
                    text("""UPDATE effect_intents
                      SET status=:status, evidence_ids=:evidence,
                          state_revision=state_revision+1, external_ref=:eref,
                          updated_at=clock_timestamp()
                      WHERE id=:id
                      RETURNING *"""),
                    {
                        "id": effect_id,
                        "status": new_status,
                        "evidence": evidence,
                        "eref": body.external_ref,
                    },
                )
                .mappings()
                .one()
            )
            # BudgetUsage：APPLIED→SUCCEEDED 计入 tool/network/disk（幂等旧回执已提前返回）
            if new_status == "SUCCEEDED" and meta["goal_id"] is not None:
                from ..domain.tool_capability_manifest import TOOL_CAPABILITY_MANIFESTS
                from .budget_clock import record_goal_budget_meters

                network_delta = 0
                tool_ref = str(effect["tool_ref"])
                manifest = TOOL_CAPABILITY_MANIFESTS.get(tool_ref)
                if (manifest is not None and manifest.url_param_keys) or tool_ref in {
                    "http_fetch",
                    "outbound_http",
                }:
                    network_delta = 1

                artifact_ids = [
                    *(body.result_artifact_ids or []),
                    *([body.stdout_artifact_id] if body.stdout_artifact_id else []),
                    *([body.stderr_artifact_id] if body.stderr_artifact_id else []),
                ]
                disk_delta = 0
                if artifact_ids:
                    sizes = db.execute(
                        text(
                            """SELECT COALESCE(SUM(size_bytes), 0) AS total
                            FROM artifacts WHERE id = ANY(:ids)"""
                        ),
                        {"ids": artifact_ids},
                    ).scalar()
                    disk_delta = int(sizes or 0)

                record_goal_budget_meters(
                    db,
                    meta["goal_id"],
                    tool_calls_delta=1,
                    network_calls_delta=network_delta,
                    disk_bytes_delta=disk_delta,
                )
        else:
            # 失租/迟到/参数矛盾：入库但不推进业务状态，等待核对。
            disposition = "PENDING_RECONCILIATION"

        db.execute(
            text("""INSERT INTO effect_receipts(
              receipt_id,effect_id,project_id,producer_activity_id,producer_attempt_id,
              fencing_epoch,started_at,finished_at,exit_code,signal,timed_out,
              stdout_artifact_id,stderr_artifact_id,result_artifact_ids,external_ref,
              observed_outcome,disposition,content_digest)
            VALUES(
              :receipt,:effect,:project,:activity,:attempt,
              :epoch,:started,:finished,:exit,:signal,:timed,
              :stdout,:stderr,:results,:eref,
              :outcome,:disposition,:digest)"""),
            {
                "receipt": body.receipt_id,
                "effect": effect_id,
                "project": effect["project_id"],
                "activity": body.producer_activity_id,
                "attempt": body.producer_attempt_id,
                "epoch": int(body.fencing_epoch),
                "started": body.started_at,
                "finished": body.finished_at,
                "exit": body.exit_code,
                "signal": body.signal,
                "timed": body.timed_out,
                "stdout": body.stdout_artifact_id,
                "stderr": body.stderr_artifact_id,
                "results": body.result_artifact_ids,
                "eref": body.external_ref,
                "outcome": body.observed_outcome,
                "disposition": disposition,
                "digest": digest,
            },
        )
        # Ledger：仅受信回执生成 EvidenceEnvelope，不接受模型自报结果。
        from .evidence import ingest_from_receipt

        ingest_from_receipt(
            db,
            effect=effect,
            body=body,
            producer_identity=f"worker:{subject}",
        )
        goal_id = db.execute(
            text("SELECT goal_id FROM activities WHERE id=:id"),
            {"id": body.producer_activity_id},
        ).scalar()
        task_id = db.execute(
            text("SELECT task_id FROM activities WHERE id=:id"),
            {"id": body.producer_activity_id},
        ).scalar()
        if goal_id is not None:
            from .control_commands import maybe_complete_pause_or_cancel
            from .finalization import try_seal_draining_barrier

            maybe_complete_pause_or_cancel(db, goal_id)
            # 在途/UNKNOWN 排空后尝试 DRAINING→SEALED（doc/01 §9；≠DONE）
            if disposition == "APPLIED" and body.observed_outcome in (
                "SUCCEEDED",
                "FAILED",
            ):
                try_seal_draining_barrier(db, goal_id)
        if task_id is not None:
            from .task_commands import maybe_complete_task_cancel

            maybe_complete_task_cancel(db, task_id)
        return (
            ReceiptAccepted(receipt_id=body.receipt_id, disposition=disposition),
            _effect_from_row(effect),
        )
