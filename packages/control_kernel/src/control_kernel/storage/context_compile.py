"""从固定 Activity binding 编译并持久化 ContextBundle。

事务外读取快照 → ContextCompiler → 物化合同工件 → 写入 context_bundles。
合同/Skill 在编译期间失效则拒绝，禁止调用模型。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

from context_compiler import (
    ActivitySnapshot,
    CompileRejected,
    InputArtifactRef,
    TokenBudget,
    compile_context_bundle,
)
from context_compiler.snapshot import role_for_kind, skill_allowed_for_role
from evidence_ledger.content import encode
from evidence_ledger.objects import S3Objects
from sqlalchemy import Engine, text

from ..protocols.runtime import LeaseRejected, WorkerForbidden
from .artifacts import Artifacts
from .plan_inputs import assert_attempt_plan_input
from .policies import ScopeNotFound, TrustBlocked
from .probe import _content_digest, create_context_bundle


def _ensure_artifact(
    engine: Engine,
    objects: S3Objects,
    project_id: UUID,
    digest: str,
    payload: bytes,
    producer_identity: str,
) -> UUID:
    """按 digest 幂等物化工件；并发/重试下容忍唯一键冲突。"""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    with engine.connect() as db:
        existing = db.execute(
            text(
                """SELECT id FROM artifacts
                WHERE project_id=:project AND digest=:digest
                ORDER BY created_at,id LIMIT 1"""
            ),
            {"project": project_id, "digest": digest},
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    # Kernel 物化合同可用部署上限；测试 fixture 的 1KiB 仅约束手工小样例。
    store = objects
    if len(payload) > objects.max_bytes:
        store = S3Objects(
            objects.client,
            objects.bucket,
            max_bytes=max(objects.max_bytes, len(payload), 16 * 1024 * 1024),
        )
    try:
        return (
            Artifacts(engine, store)
            .ingest_raw(
                project_id,
                digest,
                BytesIO(payload),
                mime="application/json",
                producer_identity=producer_identity,
            )
            .id
        )
    except SAIntegrityError:
        # Temporal 重试 / 并发 compile：同 digest 已由他方写入
        with engine.connect() as db:
            existing = db.execute(
                text(
                    """SELECT id FROM artifacts
                    WHERE project_id=:project AND digest=:digest
                    ORDER BY created_at,id LIMIT 1"""
                ),
                {"project": project_id, "digest": digest},
            ).scalar_one_or_none()
        if existing is not None:
            return existing
        raise


def _goal_contract_payload(db, goal: dict) -> bytes:
    edges = db.execute(
        text(
            """SELECT target_type,target_id,target_digest FROM reference_edges
            WHERE project_id=:project AND source_type='GoalContract'
              AND source_id=:source AND source_digest=:digest
            ORDER BY target_type,target_id"""
        ),
        {
            "project": goal["project_id"],
            "source": str(goal["id"]),
            "digest": goal["contract_digest"],
        },
    ).mappings()
    bindings = [
        {
            "object_type": row["target_type"],
            "object_id": str(row["target_id"]),
            "content_digest": row["target_digest"],
        }
        for row in edges
    ]
    payload = encode(
        json.dumps(
            {
                "schema_version": 3,
                "object_type": "GoalContract",
                "content": goal["contract"],
                "reference_bindings": bindings,
            }
        )
    )
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if digest != goal["contract_digest"]:
        raise CompileRejected("GoalContract 无法复现 contract_digest，拒绝编译")
    return payload


def _load_compile_inputs(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    lease_activity_id: UUID,
    attempt_id: UUID,
    fencing_epoch: str,
) -> dict:
    if lease_activity_id != activity_id:
        raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
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
        attempt = (
            db.execute(
                text(
                    """SELECT * FROM activity_attempts
                    WHERE id=:id AND activity_id=:activity"""
                ),
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
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能编译上下文")

        activity = dict(
            db.execute(text("SELECT * FROM activities WHERE id=:id"), {"id": activity_id})
            .mappings()
            .one()
        )
        # doc/05 §3.11：Trust BLOCKED 拒新 context 绑定/编译（允许回执/停止/对账，不允许新推理输入）
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

            assert_goal_allows_new_inference(db, activity["goal_id"])
        role = role_for_kind(activity["kind"])
        binding = dict(activity["binding"] or {})
        prior_attempts = int(
            db.execute(
                text(
                    """SELECT COUNT(*) FROM activity_attempts
                    WHERE activity_id=:id AND status <> 'ACTIVE'"""
                ),
                {"id": activity_id},
            ).scalar_one()
        )

        skill_bindings: list[InputArtifactRef] = []
        skill_version_rows: list[dict] = []
        excluded: list[tuple[str, str]] = []
        contract_bytes: bytes | None = None
        contract_digest: str | None = None
        goal_id = activity["goal_id"]
        candidate_snapshot: tuple[str, bytes] | None = None

        if goal_id is not None:
            goal = dict(
                db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": goal_id})
                .mappings()
                .one()
            )
            if binding.get("goal_contract_digest") != goal["contract_digest"]:
                raise LeaseRejected("BINDING_STALE", "Goal 合同已变更，须重建 Activity")
            if binding.get("goal_contract_revision") != goal["contract_revision"]:
                raise LeaseRejected("BINDING_STALE", "Goal 合同修订已变更")
            if activity["kind"] == "PLAN" and goal["plan_input_mode"] == "REQUIRED":
                selected = assert_attempt_plan_input(db, activity, attempt)
                candidate = (
                    db.execute(
                        text("SELECT reason,tasks,coverage FROM plans WHERE id=:id"),
                        {"id": selected.candidate_plan_id},
                    )
                    .mappings()
                    .one()
                )
                from ..protocols.plans import PlanCreate

                plan = PlanCreate.model_validate(
                    {
                        "expected_plan_revision": selected.expected_plan_revision,
                        "reason": candidate["reason"],
                        "tasks": candidate["tasks"],
                        "coverage": candidate["coverage"],
                    }
                )
                raw = json.dumps(
                    {
                        "goal_id": str(goal_id),
                        "goal_contract_revision": goal["contract_revision"],
                        "plan": plan.model_dump(mode="json"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                if "sha256:" + hashlib.sha256(raw).hexdigest() != selected.candidate_content_digest:
                    raise CompileRejected("固定候选正文摘要漂移")
                candidate_snapshot = (selected.candidate_content_digest, raw)

            contract_bytes = _goal_contract_payload(db, goal)
            contract_digest = goal["contract_digest"]

            skill_set = dict(
                db.execute(
                    text("SELECT * FROM skill_sets WHERE id=:id"),
                    {"id": goal["contract"]["skill_set_id"]},
                )
                .mappings()
                .one()
            )
            if binding.get("skill_set_digest") not in (None, skill_set["content_digest"]):
                raise LeaseRejected("BINDING_STALE", "SkillSet 已变更")

            for version_id in skill_set["config"].get("skill_version_ids") or []:
                skill = dict(
                    db.execute(
                        text("SELECT * FROM skill_versions WHERE id=:id"),
                        {"id": version_id},
                    )
                    .mappings()
                    .one()
                )
                if skill["status"] != "ACTIVE":
                    raise CompileRejected(f"SkillVersion 非 ACTIVE: {version_id}")
                edge = (
                    db.execute(
                        text(
                            """SELECT target_id,target_digest FROM reference_edges
                            WHERE source_type='SkillVersion' AND source_id=:source
                              AND target_type='Artifact'
                            ORDER BY target_id LIMIT 1"""
                        ),
                        {"source": str(version_id)},
                    )
                    .mappings()
                    .first()
                )
                if edge is None:
                    raise CompileRejected(f"SkillVersion 缺少 Artifact 边: {version_id}")
                scopes = list(skill["config"].get("role_scopes") or [])
                if not skill_allowed_for_role(role, scopes):
                    continue
                skill_bindings.append(
                    InputArtifactRef(
                        artifact_id=edge["target_id"],
                        digest=edge["target_digest"],
                        classification="SKILL",
                    )
                )
                skill_version_rows.append(
                    {
                        "version_id": str(version_id),
                        "content_digest": skill["content_digest"],
                    }
                )

            if role == "EXECUTOR":
                for criterion in goal["contract"].get("success_criteria") or []:
                    profile_id = criterion.get("verification_profile_id")
                    if profile_id is None:
                        continue
                    layer = db.execute(
                        text(
                            """SELECT config->>'layer' FROM verification_profiles
                            WHERE id=:id"""
                        ),
                        {"id": profile_id},
                    ).scalar_one_or_none()
                    if layer == "HOLDOUT":
                        excluded.append((f"criterion:{criterion.get('id')}", "HOLDOUT_ACL"))

        feedback_ids: list[UUID] = []
        review_payloads: list[tuple[str, bytes]] = []
        critic_snapshot: tuple[str, bytes] | None = None
        if role == "PLANNER" and goal_id is not None:
            from .feedback import feedback_ids_for_planner

            feedback_ids = feedback_ids_for_planner(
                db,
                goal_id,
                goal_contract_revision=binding.get("goal_contract_revision"),
                plan_revision=binding.get("plan_revision"),
            )
            from .audits import goal_review_payloads_for_planner

            review_payloads = goal_review_payloads_for_planner(
                db,
                goal_id,
                goal_contract_revision=binding.get("goal_contract_revision"),
                plan_revision=binding.get("plan_revision"),
            )
        if (
            role == "AUDITOR"
            and activity["kind"] == "AUDIT"
            and activity["target_type"] == "GOAL_REVIEW"
        ):
            from .audits import review_snapshot_bytes

            snap_digest = binding.get("subject_digest")
            if not snap_digest:
                raise CompileRejected("GOAL_REVIEW 活动缺少 review_snapshot_digest")
            raw = review_snapshot_bytes(db, snap_digest)
            if raw is None:
                raise CompileRejected("权威 GoalReview 快照缺失，拒绝 AUDITOR 编译（失败关闭）")
            critic_snapshot = (snap_digest, raw)

        return {
            "project_id": activity["project_id"],
            "activity_id": activity_id,
            "attempt_id": attempt_id,
            "role": role,
            "goal_id": goal_id,
            "binding": binding,
            "prior_attempts": prior_attempts,
            "skill_bindings": skill_bindings,
            "skill_version_rows": skill_version_rows,
            "excluded": excluded,
            "contract_bytes": contract_bytes,
            "contract_digest": contract_digest,
            "feedback_ids": feedback_ids,
            "review_payloads": review_payloads,
            "critic_snapshot": critic_snapshot,
            "candidate_snapshot": candidate_snapshot,
        }


def compile_and_persist_context(
    engine: Engine,
    objects: S3Objects | None,
    subject: str,
    activity_id: UUID,
    lease_activity_id: UUID,
    attempt_id: UUID,
    fencing_epoch: str,
    budget: TokenBudget | None = None,
) -> tuple[UUID, str, dict]:
    if objects is None:
        raise RuntimeError("object storage not configured")

    loaded = _load_compile_inputs(
        engine, subject, activity_id, lease_activity_id, attempt_id, fencing_epoch
    )

    input_refs: list[InputArtifactRef] = []
    if loaded["contract_bytes"] is not None and loaded["contract_digest"] is not None:
        artifact_id = _ensure_artifact(
            engine,
            objects,
            loaded["project_id"],
            loaded["contract_digest"],
            loaded["contract_bytes"],
            producer_identity="kernel:context-compiler",
        )
        input_refs.append(
            InputArtifactRef(
                artifact_id=artifact_id,
                digest=loaded["contract_digest"],
                classification="CONTRACT",
            )
        )
    input_refs.extend(loaded["skill_bindings"])
    candidate_snapshot = loaded["candidate_snapshot"]
    if candidate_snapshot is not None:
        candidate_digest, candidate_bytes = candidate_snapshot
        artifact_id = _ensure_artifact(
            engine,
            objects,
            loaded["project_id"],
            candidate_digest,
            candidate_bytes,
            producer_identity="kernel:plan-input-candidate",
        )
        read_store = objects
        if len(candidate_bytes) > objects.max_bytes:
            read_store = S3Objects(
                objects.client,
                objects.bucket,
                max_bytes=max(objects.max_bytes, len(candidate_bytes), 16 * 1024 * 1024),
            )
        if read_store.read(loaded["project_id"], candidate_digest) != candidate_bytes:
            raise CompileRejected("对象仓候选字节与固定输入不一致")
        input_refs.append(
            InputArtifactRef(
                artifact_id=artifact_id,
                digest=candidate_digest,
                classification="CANDIDATE",
            )
        )

    # PLANNER：GoalReview 事实摘要以 EVIDENCE 绑定进入清单（≠ planning_feedback）
    for digest, payload in loaded.get("review_payloads") or ():
        artifact_id = _ensure_artifact(
            engine,
            objects,
            loaded["project_id"],
            digest,
            payload,
            producer_identity="kernel:goal-review",
        )
        input_refs.append(
            InputArtifactRef(
                artifact_id=artifact_id,
                digest=digest,
                classification="EVIDENCE",
            )
        )

    # AUDITOR×GOAL_REVIEW：权威运行快照以 EVIDENCE 绑定（ensure 时钉扎）
    critic = loaded.get("critic_snapshot")
    if critic is not None:
        snap_digest, snap_bytes = critic
        artifact_id = _ensure_artifact(
            engine,
            objects,
            loaded["project_id"],
            snap_digest,
            snap_bytes,
            producer_identity="kernel:goal-review-snapshot",
        )
        input_refs.append(
            InputArtifactRef(
                artifact_id=artifact_id,
                digest=snap_digest,
                classification="EVIDENCE",
            )
        )

    binding = loaded["binding"]
    snapshot = ActivitySnapshot(
        project_id=loaded["project_id"],
        activity_id=loaded["activity_id"],
        role=loaded["role"],
        goal_id=loaded["goal_id"],
        goal_contract_revision=binding.get("goal_contract_revision"),
        task_contract_revision=binding.get("task_contract_revision"),
        plan_revision=binding.get("plan_revision"),
        input_bindings=tuple(input_refs),
        excluded_refs=tuple(loaded["excluded"]),
        session_generation=loaded["prior_attempts"],
        compaction_source_digest=None,
        planning_feedback_ids=tuple(loaded.get("feedback_ids") or ()),
    )
    content = compile_context_bundle(snapshot, budget)
    # 硬不变量：GoalReview / Critic 快照仅以 EVIDENCE 进入清单
    review_digests = {d for d, _ in loaded.get("review_payloads") or ()}
    if loaded.get("critic_snapshot") is not None:
        review_digests.add(loaded["critic_snapshot"][0])
    if loaded["role"] not in ("PLANNER", "AUDITOR") and review_digests:
        raise CompileRejected("非 PLANNER/AUDITOR 不得绑定 GoalReview EVIDENCE")
    evidence_digests = {
        b["digest"] for b in content["input_bindings"] if b["classification"] == "EVIDENCE"
    }
    if not review_digests <= evidence_digests:
        raise CompileRejected("GoalReview/快照 digest 未完整进入 EVIDENCE 绑定")
    _content_digest("ContextBundle", content)

    bundle_id, digest = create_context_bundle(
        engine,
        subject,
        activity_id,
        lease_activity_id,
        attempt_id,
        fencing_epoch,
        content,
        candidate_verified=candidate_snapshot is not None,
    )
    if loaded["skill_version_rows"]:
        with engine.begin() as db:
            db.execute(
                text(
                    """UPDATE activity_attempts
                    SET skill_versions=CAST(:skills AS jsonb), updated_at=clock_timestamp()
                    WHERE id=:id AND status='ACTIVE'"""
                ),
                {
                    "id": attempt_id,
                    "skills": json.dumps(loaded["skill_version_rows"]),
                },
            )
    return bundle_id, digest, content
