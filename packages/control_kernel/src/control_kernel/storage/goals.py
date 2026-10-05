"""Goal 持久化：创建 DRAFT 合同；不启动调度、不宣称 DONE。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.goals import BudgetUsage, GoalContractUpdate, GoalCreate, GoalResource
from ..protocols.runtime import InvalidGoalState, StateRevisionConflict
from .policies import ConfigurationVersions, InvalidConfiguration, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict


def acquire_goal_admission_lock(db, goal_id: UUID) -> None:
    """获取 Goal 级 finalization/admission 事务锁。

    串行化：expire→Stop/UNKNOWN、DONE 前就绪检查、effect/Stop 回执咽喉。
    **锁序硬约束**：本锁必须先于该 Goal 下 activity/attempt/stops 行锁，
    避免 finalize（admission→activity→attempt）与 expire（attempt→admission）死锁。
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
        {"scope": f"goal-admission:{goal_id}"},
    )


def default_orchestration_backend(
    environ: Mapping[str, str] | None = None,
) -> str:
    """创建 Goal 时的默认编排后端。

    仅当 ``RING_ORCHESTRATION_DEFAULT_BACKEND=TEMPORAL`` 时写入 TEMPORAL；
    或当 ``RING_ORCHESTRATION_REQUIRE_TEMPORAL`` 且已配置 ``RING_TEMPORAL_TARGET`` 时强制 TEMPORAL。
    缺省或非法值均为 LEGACY，避免测试默认被翻转。
    """
    env = os.environ if environ is None else environ
    if temporal_orchestration_enforced(env):
        return "TEMPORAL"
    raw = (env.get("RING_ORCHESTRATION_DEFAULT_BACKEND") or "LEGACY").strip().upper()
    if raw == "TEMPORAL":
        return "TEMPORAL"
    return "LEGACY"


def temporal_target_configured(environ: Mapping[str, str] | None = None) -> bool:
    """是否已配置可连的 Temporal 目标地址（仅检查非空，不探活）。"""
    env = os.environ if environ is None else environ
    return bool((env.get("RING_TEMPORAL_TARGET") or "").strip())


def temporal_orchestration_required(environ: Mapping[str, str] | None = None) -> bool:
    """是否显式要求 TEMPORAL 编排（排空 LEGACY 闸门）。"""
    env = os.environ if environ is None else environ
    raw = (env.get("RING_ORCHESTRATION_REQUIRE_TEMPORAL") or "").strip().lower()
    return raw in ("1", "true", "yes")


def temporal_orchestration_enforced(environ: Mapping[str, str] | None = None) -> bool:
    """REQUIRE_TEMPORAL 且已配置 TARGET 时生效；缺 TARGET 不静默假强制。"""
    env = os.environ if environ is None else environ
    return temporal_orchestration_required(env) and temporal_target_configured(env)


def legacy_claim_drain_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """强制 TEMPORAL 时是否仍允许全局 claim 领取 LEGACY Goal（显式排空）。

    仅 ``RING_LEGACY_CLAIM_DRAIN=1``（或 true/yes）开启；默认关闭以防双调度。
    未强制 TEMPORAL 时本开关无意义（claim 仍按 LEGACY/TEMPORAL 分拣）。
    """
    env = os.environ if environ is None else environ
    raw = (env.get("RING_LEGACY_CLAIM_DRAIN") or "").strip().lower()
    return raw in ("1", "true", "yes")


class LegacyOrchestrationForbidden(Exception):
    """已强制 TEMPORAL 编排时禁止再 START LEGACY Goal（防双调度）。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _row_to_resource(row, barrier=None) -> GoalResource:
    data = dict(row)
    return GoalResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "project_id": data["project_id"],
            "status": data["status"],
            "state_revision": data["state_revision"],
            "contract_revision": data["contract_revision"],
            "plan_revision": data["plan_revision"],
            "plan_input_mode": data.get("plan_input_mode") or "OPTIONAL",
            "contract": data["contract"],
            "contract_digest": data["contract_digest"],
            "previous_status": data["previous_status"],
            "integration_commit": data["integration_commit"],
            "criterion_summary": {
                "verified": data["criterion_verified"],
                "total": data["criterion_total"],
            },
            "budget_usage": data["budget_usage"],
            "block_reason": data["block_reason"],
            "write_epoch": data["write_epoch"],
            "orchestration_backend": data.get("orchestration_backend") or "LEGACY",
            "owner_epoch": data.get("owner_epoch") or "1",
            "barrier": barrier,
            "release_manifest_id": data["release_manifest_id"],
        }
    )


class Goals:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(
        self,
        subject: str,
        project_ids: list[str],
        key: str,
        body: GoalCreate,
        digest: str,
        canonical: dict,
        bindings: list[dict],
    ) -> GoalResource:
        path = "/api/v1/goals"
        request_scope = json.dumps(
            [str(body.project_id), subject, "POST", path, key], separators=(",", ":")
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
                return GoalResource.model_validate(old["result"])
            if trust != "OPEN":
                raise TrustBlocked()
            for table, oid in (
                ("policies", body.policy_id),
                ("model_profiles", body.model_profile_id),
                ("skill_sets", body.skill_set_id),
            ):
                exists = db.execute(
                    text(f"SELECT 1 FROM {table} WHERE project_id=:project AND id=:id"),
                    {"project": body.project_id, "id": oid},
                ).first()
                if exists is None:
                    raise InvalidConfiguration(f"missing {table}")
            from .skills import assert_skill_set_versions_active

            assert_skill_set_versions_active(
                db, project_id=body.project_id, skill_set_id=body.skill_set_id
            )
            for criterion in body.success_criteria:
                profile = (
                    db.execute(
                        text(
                            """SELECT config FROM verification_profiles
                            WHERE project_id=:project AND id=:id"""
                        ),
                        {
                            "project": body.project_id,
                            "id": criterion.verification_profile_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                if profile is None:
                    raise InvalidConfiguration("missing verification profile")
                # Goal 标准可绑定 TASK/GOAL/SKILL 配置，但必须同项目已登记。
            now = datetime.now(UTC)
            usage = BudgetUsage(
                consumed_tokens=0,
                reserved_tokens=0,
                consumed_cost_usd="0",
                reserved_cost_usd="0",
                cost_status="CONFIRMED",
                elapsed_wall_seconds=0,
                active_seconds=0,
                tool_calls=0,
                network_calls=0,
                disk_bytes=0,
                gpu_seconds=None,
                observed_at=now,
            )
            goal_id = uuid4()
            # 默认 LEGACY；仅 env=TEMPORAL 时在 INSERT 写入 TEMPORAL（无 LEGACY fallback 语义）
            orch_backend = default_orchestration_backend()
            row = (
                db.execute(
                    text("""INSERT INTO goals(
                      id,project_id,status,state_revision,contract_revision,plan_revision,
                      contract,contract_digest,previous_status,integration_commit,
                      criterion_verified,criterion_total,budget_usage,block_reason,write_epoch,
                      orchestration_backend,owner_epoch,release_manifest_id)
                    VALUES(
                      :id,:project,'DRAFT',1,1,NULL,CAST(:contract AS jsonb),:digest,NULL,NULL,
                      0,:total,CAST(:usage AS jsonb),NULL,'1',:orch_backend,'1',NULL)
                    RETURNING *"""),
                    {
                        "id": goal_id,
                        "project": body.project_id,
                        "contract": json.dumps(canonical),
                        "digest": digest,
                        "total": len(body.success_criteria),
                        "usage": usage.model_dump_json(),
                        "orch_backend": orch_backend,
                    },
                )
                .mappings()
                .one()
            )
            result = _row_to_resource(row)
            for binding in bindings:
                db.execute(
                    text(
                        """INSERT INTO reference_edges
                        (project_id,source_type,source_id,source_digest,target_type,target_id,target_digest)
                        VALUES(:project,'GoalContract',:source,:digest,:target_type,:target,:target_digest)"""
                    ),
                    {
                        "project": body.project_id,
                        "source": str(result.id),
                        "digest": digest,
                        "target_type": binding["object_type"],
                        "target": binding["object_id"],
                        "target_digest": binding["content_digest"],
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
                    "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'GOAL_CREATED',CAST(:payload AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "project": body.project_id,
                    "payload": result.model_dump_json(),
                },
            )
            return result

    def update_contract(
        self,
        goal_id: UUID,
        subject: str,
        project_ids: list[str],
        key: str,
        body: GoalContractUpdate,
        digest: str,
        canonical: dict,
        bindings: list[dict],
    ) -> GoalResource:
        path = f"/api/v1/goals/{goal_id}/contract"
        request_scope = json.dumps(
            [str(goal_id), subject, "PUT", path, key], separators=(",", ":")
        )
        request_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        with self.engine.begin() as db:
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
                return GoalResource.model_validate(old["result"])
            if trust != "OPEN":
                raise TrustBlocked()
            if goal["status"] not in ("DRAFT", "PAUSED"):
                raise InvalidGoalState(goal["status"])
            if goal["state_revision"] != body.expected_state_revision:
                raise StateRevisionConflict()
            if body.contract.project_id != goal["project_id"]:
                raise InvalidConfiguration("project_id 不可变更")
            for table, oid in (
                ("policies", body.contract.policy_id),
                ("model_profiles", body.contract.model_profile_id),
                ("skill_sets", body.contract.skill_set_id),
            ):
                exists = db.execute(
                    text(f"SELECT 1 FROM {table} WHERE project_id=:project AND id=:id"),
                    {"project": goal["project_id"], "id": oid},
                ).first()
                if exists is None:
                    raise InvalidConfiguration(f"missing {table}")
            from .skills import assert_skill_set_versions_active

            assert_skill_set_versions_active(
                db,
                project_id=goal["project_id"],
                skill_set_id=body.contract.skill_set_id,
            )
            for criterion in body.contract.success_criteria:
                profile = (
                    db.execute(
                        text(
                            """SELECT 1 FROM verification_profiles
                            WHERE project_id=:project AND id=:id"""
                        ),
                        {
                            "project": goal["project_id"],
                            "id": criterion.verification_profile_id,
                        },
                    )
                    .first()
                )
                if profile is None:
                    raise InvalidConfiguration("missing verification profile")
            # 改合同后旧图不适用：清空 plan_revision 标记，resume 须重规划。
            updated = (
                db.execute(
                    text(
                        """UPDATE goals SET
                          contract=CAST(:contract AS jsonb),
                          contract_digest=:digest,
                          contract_revision=contract_revision+1,
                          state_revision=state_revision+1,
                          plan_revision=NULL,
                          criterion_verified=0,
                          criterion_total=:total,
                          block_reason=:reason,
                          updated_at=clock_timestamp()
                        WHERE id=:id AND state_revision=:rev
                        RETURNING *"""
                    ),
                    {
                        "id": goal_id,
                        "contract": json.dumps(canonical),
                        "digest": digest,
                        "total": len(body.contract.success_criteria),
                        "reason": f"合同已更新: {body.reason}"[:2000],
                        "rev": body.expected_state_revision,
                    },
                )
                .mappings()
                .first()
            )
            if updated is None:
                raise StateRevisionConflict()
            # 未发布的候选计划标为 REJECTED，避免误采纳旧合同下的提案。
            db.execute(
                text(
                    """UPDATE plans SET status='REJECTED', updated_at=clock_timestamp()
                    WHERE goal_id=:goal AND status='CANDIDATE'"""
                ),
                {"goal": goal_id},
            )
            from .finalization import get_barrier_for_goal

            result = _row_to_resource(updated, get_barrier_for_goal(db, goal_id))
            for binding in bindings:
                db.execute(
                    text(
                        """INSERT INTO reference_edges
                        (project_id,source_type,source_id,source_digest,target_type,target_id,target_digest)
                        VALUES(:project,'GoalContract',:source,:digest,:target_type,:target,:target_digest)"""
                    ),
                    {
                        "project": goal["project_id"],
                        "source": str(goal_id),
                        "digest": digest,
                        "target_type": binding["object_type"],
                        "target": binding["object_id"],
                        "target_digest": binding["content_digest"],
                    },
                )
            db.execute(
                text(
                    "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
                ),
                {
                    "scope": request_scope,
                    "digest": request_digest,
                    "result": result.model_dump_json(),
                },
            )
            db.execute(
                text(
                    "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'GOAL_CONTRACT_UPDATED',CAST(:payload AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "project": goal["project_id"],
                    "payload": result.model_dump_json(),
                },
            )
            from .events import append_goal_event

            append_goal_event(
                db,
                project_id=goal["project_id"],
                goal_id=goal_id,
                event_type="GOAL_STATE_CHANGED",
                entity_id=goal_id,
                entity_state_revision=updated["state_revision"],
                resource_type="GOAL",
            )
            return result

    def get(self, goal_id: UUID, subject: str, project_ids: list[str]) -> GoalResource:
        with self.engine.connect() as db:
            row = (
                db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": goal_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
            from .finalization import get_barrier_for_goal

            return _row_to_resource(row, get_barrier_for_goal(db, goal_id))

    def get_for_worker(self, goal_id: UUID, subject: str) -> GoalResource:
        """已登记 ACTIVE worker 且对该 Goal 下某活动持有 ACTIVE attempt 时可读合同。

        供 RunActivation 构造 PlanCreate 骨架（任务结构来自合同，非模型编造）。
        """
        from ..protocols.runtime import WorkerForbidden

        with self.engine.connect() as db:
            worker = (
                db.execute(
                    text(
                        "SELECT id FROM workers WHERE subject=:subject AND status='ACTIVE'"
                    ),
                    {"subject": subject},
                )
                .mappings()
                .first()
            )
            if worker is None:
                raise WorkerForbidden()
            row = (
                db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": goal_id})
                .mappings()
                .first()
            )
            if row is None:
                raise ScopeNotFound()
            held = db.execute(
                text(
                    """SELECT 1 FROM activity_attempts aa
                    INNER JOIN activities a ON a.id = aa.activity_id
                    WHERE a.goal_id=:gid AND aa.worker_id=:wid AND aa.status='ACTIVE'
                    LIMIT 1"""
                ),
                {"gid": goal_id, "wid": worker["id"]},
            ).first()
            if held is None:
                raise ScopeNotFound()
            from .finalization import get_barrier_for_goal

            return _row_to_resource(row, get_barrier_for_goal(db, goal_id))

    def list_goals(
        self,
        project_id: UUID,
        subject: str,
        project_ids: list[str],
        limit: int,
        after: UUID | None,
        status: str | None,
    ) -> list[GoalResource]:
        with self.engine.connect() as db:
            ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
            rows = db.execute(
                text("""SELECT * FROM goals WHERE project_id=:project
                AND (CAST(:status AS text) IS NULL OR status=CAST(:status AS text))
                AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                  SELECT created_at,id FROM goals WHERE id=CAST(:after AS uuid) AND project_id=:project))
                ORDER BY created_at,id LIMIT :limit"""),
                {
                    "project": project_id,
                    "after": after,
                    "limit": limit,
                    "status": status,
                },
            ).mappings()
            return [_row_to_resource(row) for row in rows]


# 供路由侧构造 content_digest 时查找依赖摘要。
def lookup_config_digests(engine: Engine, project_id: UUID, body: GoalCreate) -> list[dict]:
    with engine.connect() as db:
        policy = db.execute(
            text("SELECT content_digest FROM policies WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": body.policy_id},
        ).scalar_one_or_none()
        model = db.execute(
            text("SELECT content_digest FROM model_profiles WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": body.model_profile_id},
        ).scalar_one_or_none()
        skill_set = db.execute(
            text("SELECT content_digest FROM skill_sets WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": body.skill_set_id},
        ).scalar_one_or_none()
        if policy is None or model is None or skill_set is None:
            raise InvalidConfiguration("missing goal configuration")
        bindings = [
            {
                "object_type": "Policy",
                "object_id": str(body.policy_id),
                "content_digest": policy,
            },
            {
                "object_type": "ModelProfile",
                "object_id": str(body.model_profile_id),
                "content_digest": model,
            },
            {
                "object_type": "SkillSet",
                "object_id": str(body.skill_set_id),
                "content_digest": skill_set,
            },
        ]
        seen: set[UUID] = set()
        for criterion in body.success_criteria:
            if criterion.verification_profile_id in seen:
                continue
            seen.add(criterion.verification_profile_id)
            digest = db.execute(
                text(
                    "SELECT content_digest FROM verification_profiles WHERE project_id=:p AND id=:id"
                ),
                {"p": project_id, "id": criterion.verification_profile_id},
            ).scalar_one_or_none()
            if digest is None:
                raise InvalidConfiguration("missing verification profile")
            bindings.append(
                {
                    "object_type": "VerificationProfile",
                    "object_id": str(criterion.verification_profile_id),
                    "content_digest": digest,
                }
            )
        return bindings


def content_digest_for(body: GoalCreate, bindings: list[dict]) -> tuple[str, dict]:
    from evidence_ledger.content import encode

    canonical = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": "GoalContract",
                "content": body.model_dump(mode="json"),
                "reference_bindings": bindings,
            }
        )
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest(), json.loads(canonical)["content"]


class UnsafeOrchestrationBackendSwitch(Exception):
    """Goal 仍有在途 attempt/effect 或未排空 READY/RUNNING，禁止切换编排后端。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def set_goal_orchestration_backend(
    engine: Engine,
    goal_id: UUID,
    backend: str,
    *,
    owner_epoch: str = "1",
    force: bool = False,
) -> None:
    """测试/排空辅助：翻转 Goal 编排后端。公共 create 不暴露 TEMPORAL，避免误开双调度。

    拒绝条件（防双派发 / 未排空回退）：
    - 任一对应该 Goal 的 ACTIVE attempt；
    - 任一 DISPATCHED / UNKNOWN effect；
    - TEMPORAL→LEGACY 且仍有 READY/RUNNING activity，且未显式 ``force=True``。

    ``force`` 仅绕过 READY/RUNNING 检查，供测试排空后回写；不能绕过 ACTIVE / 未知 effect。
    """
    if backend not in ("LEGACY", "TEMPORAL"):
        raise ValueError("backend must be LEGACY or TEMPORAL")
    with engine.begin() as db:
        current = (
            db.execute(
                text(
                    """SELECT orchestration_backend FROM goals WHERE id=:id FOR UPDATE"""
                ),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if current is None:
            raise ScopeNotFound()

        active_attempts = db.execute(
            text(
                """SELECT count(*) FROM activity_attempts att
                JOIN activities a ON a.id = att.activity_id
                WHERE a.goal_id=:goal AND att.status='ACTIVE'"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        if active_attempts:
            raise UnsafeOrchestrationBackendSwitch(
                "Goal 仍有 ACTIVE attempt，禁止切换编排后端"
            )

        open_effects = db.execute(
            text(
                """SELECT count(*) FROM effect_intents
                WHERE goal_id=:goal AND status IN ('DISPATCHED','UNKNOWN')"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        if open_effects:
            raise UnsafeOrchestrationBackendSwitch(
                "Goal 仍有 DISPATCHED/UNKNOWN effect，禁止切换编排后端"
            )

        if (
            current["orchestration_backend"] == "TEMPORAL"
            and backend == "LEGACY"
            and not force
        ):
            live = db.execute(
                text(
                    """SELECT count(*) FROM activities
                    WHERE goal_id=:goal AND status IN ('READY','RUNNING')"""
                ),
                {"goal": goal_id},
            ).scalar_one()
            if live:
                raise UnsafeOrchestrationBackendSwitch(
                    "TEMPORAL Goal 仍有 READY/RUNNING activity，未排空禁止回退 LEGACY"
                )

        updated = db.execute(
            text(
                """UPDATE goals SET orchestration_backend=:backend, owner_epoch=:epoch,
                  updated_at=clock_timestamp()
                WHERE id=:id
                RETURNING id"""
            ),
            {"id": goal_id, "backend": backend, "epoch": owner_epoch},
        ).first()
        if updated is None:
            raise ScopeNotFound()


def count_legacy_goals_with_open_work(engine: Engine) -> dict[str, int]:
    """LEGACY 退役台账：仍走旧 PG 派发面的 Goal / 开放活动计数。

    供排空与双调度审计；不翻转 backend、不 claim。TEMPORAL Goal 不计入。
    """
    with engine.connect() as db:
        legacy_goals = db.execute(
            text(
                """SELECT COUNT(*) FROM goals
                WHERE orchestration_backend = 'LEGACY'
                  AND status NOT IN ('DONE','CANCELLED','FAILED')"""
            )
        ).scalar_one()
        open_activities = db.execute(
            text(
                """SELECT COUNT(*) FROM activities a
                JOIN goals g ON g.id = a.goal_id
                WHERE g.orchestration_backend = 'LEGACY'
                  AND a.status IN ('READY','RUNNING','RECOVERING')"""
            )
        ).scalar_one()
        active_attempts = db.execute(
            text(
                """SELECT COUNT(*) FROM activity_attempts att
                JOIN activities a ON a.id = att.activity_id
                JOIN goals g ON g.id = a.goal_id
                WHERE g.orchestration_backend = 'LEGACY'
                  AND att.status = 'ACTIVE'"""
            )
        ).scalar_one()
    return {
        "legacy_goals_open": int(legacy_goals),
        "legacy_activities_open": int(open_activities),
        "legacy_attempts_active": int(active_attempts),
    }
