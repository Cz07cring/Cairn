"""候选封存与 EXECUTE outcome；不把模型自报 hash 当成权威。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.candidates import (
    CandidateManifestResource,
    CandidateSealRequest,
    ExecuteSuccessOutcome,
    WorkspaceSnapshot,
)
from ..protocols.plans import ActivityOutcomeRequest
from ..protocols.runtime import (
    ActivityResource,
    ExecutionBinding,
    LeaseRejected,
    PlanRejected,
    Resources,
    StateRevisionConflict,
    WorkerForbidden,
)
from .activities import _activity_from_row
from .policies import ScopeNotFound, TrustBlocked
from .probe import _content_digest


def _candidate_from_row(row) -> CandidateManifestResource:
    content = row["content"]
    return CandidateManifestResource.model_validate(
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "project_id": row["project_id"],
            "goal_id": row["goal_id"],
            "task_id": row["task_id"],
            "activity_id": row["activity_id"],
            "protected_baseline_digest": content["protected_baseline_digest"],
            "git_commit": content["git_commit"],
            "files": content["files"],
            "dependency_lock_digests": content["dependency_lock_digests"],
            "submodules": content["submodules"],
            "lfs_objects": content["lfs_objects"],
            "image_digests": content["image_digests"],
            "verification_profile_ids": content["verification_profile_ids"],
            "content_digest": row["content_digest"],
            "workspace_snapshot_artifact_id": row["workspace_snapshot_artifact_id"],
        }
    )


def _ensure_protected_baseline(db, goal) -> str:
    existing = (
        db.execute(
            text("""SELECT content_digest FROM protected_baselines
            WHERE goal_id=:goal AND goal_contract_revision=:rev"""),
            {"goal": goal["id"], "rev": goal["contract_revision"]},
        )
        .mappings()
        .first()
    )
    if existing:
        return existing["content_digest"]
    contract = goal["contract"]
    entries = [
        {
            "ref": "goal_contract",
            "content_digest": goal["contract_digest"],
            "kind": "CONTRACT",
        },
        {
            "ref": f"policy:{contract['policy_id']}",
            "content_digest": db.execute(
                text("SELECT content_digest FROM policies WHERE id=:id"),
                {"id": contract["policy_id"]},
            ).scalar_one(),
            "kind": "POLICY",
        },
        {
            "ref": f"skill_set:{contract['skill_set_id']}",
            "content_digest": db.execute(
                text("SELECT content_digest FROM skill_sets WHERE id=:id"),
                {"id": contract["skill_set_id"]},
            ).scalar_one(),
            "kind": "SKILL",
        },
    ]
    for criterion in contract["success_criteria"]:
        entries.append(
            {
                "ref": f"verification_profile:{criterion['verification_profile_id']}",
                "content_digest": db.execute(
                    text("SELECT content_digest FROM verification_profiles WHERE id=:id"),
                    {"id": criterion["verification_profile_id"]},
                ).scalar_one(),
                "kind": "VERIFIER",
            }
        )
    content = {
        "project_id": str(goal["project_id"]),
        "goal_id": str(goal["id"]),
        "goal_contract_revision": goal["contract_revision"],
        "entries": entries,
    }
    digest = _content_digest("ProtectedBaseline", content)
    db.execute(
        text("""INSERT INTO protected_baselines(
          id,project_id,goal_id,goal_contract_revision,content_digest,content)
        VALUES(:id,:project,:goal,:rev,:digest,CAST(:content AS jsonb))"""),
        {
            "id": uuid4(),
            "project": goal["project_id"],
            "goal": goal["id"],
            "rev": goal["contract_revision"],
            "digest": digest,
            "content": json.dumps(content),
        },
    )
    return digest


def _require_owner(db, subject: str, activity_id: UUID, lease):
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
    now = datetime.now(UTC)
    expires = attempt["lease_expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= now:
        raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能复活")
    return activity, attempt


def seal_candidate(
    engine: Engine,
    subject: str,
    body: CandidateSealRequest,
    *,
    read_artifact_bytes,
) -> CandidateManifestResource:
    """从已入库快照工件封存候选；不接受请求体自填 files hash。"""
    with engine.begin() as db:
        activity, attempt = _require_owner(
            db, subject, body.lease.activity_id, body.lease
        )
        if activity["kind"] != "EXECUTE":
            raise PlanRejected("仅 EXECUTE 可封存候选")
        pending = db.execute(
            text("""SELECT 1 FROM effect_intents
              WHERE activity_id=:activity
                AND scope='ENGINEERING'
                AND status IN ('PREPARED','AUTHORIZED','DISPATCHED','UNKNOWN')
                AND tool_ref <> 'seal_candidate'
              LIMIT 1"""),
            {"activity": activity["id"]},
        ).first()
        if pending:
            raise PlanRejected("仍有未决工程 effect，不能封存候选")
        artifact = (
            db.execute(
                text("""SELECT id, digest FROM artifacts
                WHERE id=:id AND project_id=:project"""),
                {
                    "id": body.workspace_snapshot_artifact_id,
                    "project": activity["project_id"],
                },
            )
            .mappings()
            .first()
        )
        if artifact is None:
            raise ScopeNotFound()
        raw = read_artifact_bytes(activity["project_id"], artifact["digest"])
        try:
            snapshot = WorkspaceSnapshot.model_validate_json(raw)
        except Exception as exc:
            raise PlanRejected("工作区快照内容无效") from exc
        if not snapshot.files:
            raise PlanRejected("候选快照不能为空")
        paths = [f.path for f in snapshot.files]
        if len(set(paths)) != len(paths):
            raise PlanRejected("候选文件路径必须唯一")
        # 失败关闭：每个文件 digest 必须已在对象仓（供 Auditor 物化）
        for entry in snapshot.files:
            try:
                read_artifact_bytes(activity["project_id"], entry.digest)
            except FileNotFoundError as exc:
                raise PlanRejected(f"候选文件字节未入库: {entry.path}") from exc
        goal = (
            db.execute(text("SELECT * FROM goals WHERE id=:id FOR SHARE"), {"id": activity["goal_id"]})
            .mappings()
            .one()
        )
        baseline_digest = _ensure_protected_baseline(db, goal)
        for profile_id in body.verification_profile_ids:
            found = db.execute(
                text("""SELECT 1 FROM verification_profiles
                WHERE id=:id AND project_id=:project"""),
                {"id": profile_id, "project": activity["project_id"]},
            ).first()
            if found is None:
                raise PlanRejected("verification_profile 不存在")
        content = {
            "project_id": str(activity["project_id"]),
            "goal_id": str(activity["goal_id"]),
            "task_id": str(activity["task_id"]) if activity["task_id"] else None,
            "protected_baseline_digest": baseline_digest,
            "git_commit": snapshot.git_commit,
            "files": [f.model_dump(mode="json") for f in snapshot.files],
            "dependency_lock_digests": list(snapshot.dependency_lock_digests),
            "submodules": [s.model_dump(mode="json") for s in snapshot.submodules],
            "lfs_objects": [o.model_dump(mode="json") for o in snapshot.lfs_objects],
            "image_digests": list(snapshot.image_digests),
            "verification_profile_ids": [str(i) for i in body.verification_profile_ids],
        }
        digest = _content_digest("CandidateManifest", content)
        existing = (
            db.execute(
                text("SELECT * FROM candidate_manifests WHERE content_digest=:digest"),
                {"digest": digest},
            )
            .mappings()
            .first()
        )
        if existing:
            return _candidate_from_row(existing)
        # doc/05 §3.11：BLOCKED 拒新候选封存；同 digest 幂等重放不受影响
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
            from .stops import assert_goal_allows_new_engineering_writes

            assert_goal_allows_new_engineering_writes(db, activity["goal_id"])
        row = (
            db.execute(
                text("""INSERT INTO candidate_manifests(
                  id,project_id,goal_id,task_id,activity_id,attempt_id,
                  protected_baseline_digest,content_digest,content,workspace_snapshot_artifact_id)
                VALUES(
                  :id,:project,:goal,:task,:activity,:attempt,
                  :baseline,:digest,CAST(:content AS jsonb),:snapshot)
                RETURNING *"""),
                {
                    "id": uuid4(),
                    "project": activity["project_id"],
                    "goal": activity["goal_id"],
                    "task": activity["task_id"],
                    "activity": activity["id"],
                    "attempt": attempt["id"],
                    "baseline": baseline_digest,
                    "digest": digest,
                    "content": json.dumps(content),
                    "snapshot": body.workspace_snapshot_artifact_id,
                },
            )
            .mappings()
            .one()
        )
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'CANDIDATE_SEALED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": activity["project_id"],
                "payload": json.dumps(
                    {"candidate_manifest_id": str(row["id"]), "content_digest": digest},
                    separators=(",", ":"),
                ),
            },
        )
        return _candidate_from_row(row)


def get_candidate(
    engine: Engine, candidate_id: UUID, project_ids: list[str]
) -> CandidateManifestResource:
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM candidate_manifests WHERE id=:id"),
                {"id": candidate_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        if project_ids and str(row["project_id"]) not in project_ids:
            raise ScopeNotFound()
        return _candidate_from_row(row)


def submit_execute_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if not isinstance(body.outcome, ExecuteSuccessOutcome):
        raise PlanRejected("EXECUTE outcome 形状无效")
    outcome = body.outcome
    with engine.begin() as db:
        activity, attempt = _require_owner(db, subject, activity_id, body.lease)
        if activity["kind"] != "EXECUTE":
            raise PlanRejected("非 EXECUTE 活动")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if activity["goal_id"] is not None:
            from .stops import assert_goal_allows_success_outcome

            assert_goal_allows_success_outcome(db, activity["goal_id"])
        if activity["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        pending = db.execute(
            text("""SELECT 1 FROM effect_intents
              WHERE activity_id=:activity
                AND scope='ENGINEERING'
                AND status IN ('PREPARED','AUTHORIZED','DISPATCHED','UNKNOWN')
              LIMIT 1"""),
            {"activity": activity_id},
        ).first()
        if pending:
            raise PlanRejected("仍有未决工程 effect，不能提交 EXECUTE outcome")
        candidate = (
            db.execute(
                text("""SELECT * FROM candidate_manifests
                WHERE id=:id AND activity_id=:activity"""),
                {"id": outcome.candidate_manifest_id, "activity": activity_id},
            )
            .mappings()
            .first()
        )
        if candidate is None:
            raise ScopeNotFound()
        for evidence_id in outcome.evidence_ids:
            found = db.execute(
                text("""SELECT 1 FROM artifacts
                WHERE id=:id AND project_id=:project"""),
                {"id": evidence_id, "project": activity["project_id"]},
            ).first()
            if found is None:
                raise PlanRejected("evidence 工件不存在")
        now = datetime.now(UTC)
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
        task = None
        if activity["task_id"] is not None:
            task = (
                db.execute(
                    text("""UPDATE tasks SET status='VERIFYING',
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id AND status IN ('READY','RUNNING')
                      RETURNING *"""),
                    {"id": activity["task_id"]},
                )
                .mappings()
                .first()
            )
            if task is None:
                task = (
                    db.execute(
                        text("SELECT * FROM tasks WHERE id=:id"),
                        {"id": activity["task_id"]},
                    )
                    .mappings()
                    .one()
                )
            # 为每个 acceptance 创建独立 AUDIT Activity（只读验证 scope）。
            goal = (
                db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": activity["goal_id"]})
                .mappings()
                .one()
            )
            policy_digest = db.execute(
                text("SELECT content_digest FROM policies WHERE id=:id"),
                {"id": goal["contract"]["policy_id"]},
            ).scalar_one()
            model_digest = db.execute(
                text("SELECT content_digest FROM model_profiles WHERE id=:id"),
                {"id": goal["contract"]["model_profile_id"]},
            ).scalar_one()
            skill_digest = db.execute(
                text("SELECT content_digest FROM skill_sets WHERE id=:id"),
                {"id": goal["contract"]["skill_set_id"]},
            ).scalar_one()
            contract = task["contract"]
            for acceptance in contract["acceptance"]:
                profile_id = UUID(acceptance["verification_profile_id"])
                profile_row = (
                    db.execute(
                        text(
                            """SELECT config FROM verification_profiles
                            WHERE id=:id AND project_id=:project"""
                        ),
                        {
                            "id": profile_id,
                            "project": activity["project_id"],
                        },
                    )
                    .mappings()
                    .first()
                )
                profile_config = profile_row["config"] if profile_row else {}
                # doc/05：每份 VerificationProfile 只绑定一层；从 config 读取，禁止写死 MECHANICAL
                layers = list(profile_config.get("required_layers") or ["MECHANICAL"])
                if len(layers) != 1:
                    raise PlanRejected(
                        "VerificationProfile.required_layers 必须恰好一层"
                    )
                audit_layer = layers[0]
                binding = ExecutionBinding(
                    goal_contract_revision=goal["contract_revision"],
                    goal_contract_digest=goal["contract_digest"],
                    task_contract_revision=task["contract_revision"],
                    task_contract_digest=task["contract_digest"],
                    plan_revision=goal["plan_revision"],
                    subject_digest=candidate["content_digest"],
                    policy_digest=policy_digest,
                    model_profile_digest=model_digest,
                    skill_set_digest=skill_digest,
                )
                resources = Resources(
                    cpu_millicores=100,
                    memory_bytes=268435456,
                    disk_bytes=67108864,
                    model_slots=0,
                    browser_slots=0,
                    exclusive_labels=[],
                )
                db.execute(
                    text("""INSERT INTO activities(
                      id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                      binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                      retry_count,resources)
                    VALUES(
                      :id,:project,:goal,:task,:goal,'AUDIT','CANDIDATE',:candidate,
                      CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'READY',1,'{}',0,
                      CAST(:resources AS jsonb))"""),
                    {
                        "id": uuid4(),
                        "project": activity["project_id"],
                        "goal": activity["goal_id"],
                        "task": activity["task_id"],
                        "candidate": outcome.candidate_manifest_id,
                        "binding": binding.model_dump_json(),
                        # 分配即「冻结的验收契约」，须自带审计器所需的两项身份：
                        #   subject_digest —— 审的是哪份候选；
                        #   verifier_digest —— 用哪个校验器（verification_profiles.config
                        #   的生成列，FK 到 verifier_definitions）。
                        # 二者都必须在**创建时**由 Kernel 写入：Runner 只持 worker 身份，
                        # 无权读公共 `/api/v1/verification-profiles`（需 viewer/operator，
                        # 实测 403 VERIFIER_DIGEST_FETCH_FAILED）。放宽该公共路由等于
                        # 扩大 worker 读权限；而 Kernel 建审计时本已读到此行，随分配下发
                        # 才是最小权限写法（与 FINALIZE 的 profile_digest 同模式）。
                        "assignments": json.dumps(
                            [
                                {
                                    "verification_profile_id": str(profile_id),
                                    "layer": audit_layer,
                                    "acceptance_id": acceptance["id"],
                                    "audit_round": 1,
                                    "subject_digest": candidate["content_digest"],
                                    "verifier_digest": profile_config.get("verifier_digest"),
                                }
                            ]
                        ),
                        "resources": resources.model_dump_json(),
                    },
                )
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'EXECUTE_SUCCEEDED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": activity["project_id"],
                "payload": json.dumps(
                    {
                        "activity_id": str(activity_id),
                        "candidate_manifest_id": str(outcome.candidate_manifest_id),
                        "task_id": str(activity["task_id"]) if activity["task_id"] else None,
                    },
                    separators=(",", ":"),
                ),
            },
        )
        return _activity_from_row(updated)
