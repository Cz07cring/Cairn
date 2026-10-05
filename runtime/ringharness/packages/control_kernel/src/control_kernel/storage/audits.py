"""AUDIT outcome：候选验收落盘；全集 PASS 才 Task DONE，绝不以单次 PASS 冒充 Goal DONE。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.audits import (
    AuditAggregation,
    AuditCandidateOutcome,
    AuditGoalReviewOutcome,
    AuditResource,
    CandidateAuditItem,
    GoalAuditItem,
    GoalReviewItem,
    GoalReviewResource,
    MissingAuditItem,
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
from .policies import ScopeNotFound
from .probe import _content_digest
from .verification_runs import require_matching_verifier_runs

_GOAL_REVIEW_RESOURCES = Resources(
    cpu_millicores=100,
    memory_bytes=268435456,
    disk_bytes=67108864,
    model_slots=0,
    browser_slots=0,
    exclusive_labels=[],
)


def _goal_review_from_row(row) -> GoalReviewResource:
    return GoalReviewResource.model_validate(
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "project_id": row["project_id"],
            "goal_id": row["goal_id"],
            "producer_activity_id": row["activity_id"],
            "producer_attempt_id": row["attempt_id"],
            "goal_contract_revision": row["goal_contract_revision"],
            "plan_revision": row["plan_revision"],
            "review_snapshot_digest": row["review_snapshot_digest"],
            "findings": row["findings"],
            "content_digest": row["content_digest"],
        }
    )


def build_review_snapshot(db, goal) -> tuple[str, dict]:
    """从 Goal 合同/计划/在途事实钉扎不可变诊断快照；(digest, payload)。"""
    goal_id = goal["id"]
    recent = db.execute(
        text(
            """SELECT id, kind, status FROM activities
            WHERE goal_id=:goal
            ORDER BY created_at DESC, id DESC
            LIMIT 50"""
        ),
        {"goal": goal_id},
    ).mappings()
    recent_activities = [
        {"id": str(r["id"]), "kind": r["kind"], "status": r["status"]} for r in recent
    ]
    open_effects = db.execute(
        text(
            """SELECT e.id FROM effect_intents e
            JOIN activities a ON a.id=e.activity_id
            WHERE a.goal_id=:goal
              AND e.status IN ('PREPARED','AUTHORIZED','DISPATCHED','UNKNOWN')
            ORDER BY e.id
            LIMIT 100"""
        ),
        {"goal": goal_id},
    ).scalars()
    tasks = db.execute(
        text(
            """SELECT id, status FROM tasks WHERE goal_id=:goal
            ORDER BY created_at, id LIMIT 200"""
        ),
        {"goal": goal_id},
    ).mappings()
    payload = {
        "goal_id": str(goal_id),
        "goal_contract_revision": int(goal["contract_revision"]),
        "goal_contract_digest": goal["contract_digest"],
        "plan_revision": goal["plan_revision"],
        "goal_status": goal["status"],
        "write_epoch": str(goal["write_epoch"]),
        "recent_activities": recent_activities,
        "open_effect_ids": [str(i) for i in open_effects],
        "task_statuses": [
            {"task_id": str(t["id"]), "status": t["status"]} for t in tasks
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return digest, payload


def build_review_snapshot_digest(db, goal) -> str:
    """兼容旧调用：仅返回 digest。"""
    digest, _ = build_review_snapshot(db, goal)
    return digest


def persist_review_snapshot(
    db, *, project_id: UUID, goal_id: UUID, digest: str, payload: dict
) -> None:
    """按 digest 幂等写入不可变快照行（同 digest 冲突则忽略）。"""
    db.execute(
        text(
            """INSERT INTO goal_review_snapshots(
              content_digest, project_id, goal_id, payload)
            VALUES(:digest, :project, :goal, CAST(:payload AS jsonb))
            ON CONFLICT (content_digest) DO NOTHING"""
        ),
        {
            "digest": digest,
            "project": project_id,
            "goal": goal_id,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )


def review_snapshot_bytes(db, digest: str) -> bytes | None:
    """按 digest 取权威快照 canonical 字节；缺失返回 None。"""
    row = (
        db.execute(
            text(
                """SELECT payload FROM goal_review_snapshots
                WHERE content_digest=:digest"""
            ),
            {"digest": digest},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    recomputed = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    if recomputed != digest:
        raise PlanRejected("goal_review_snapshot 无法复现 content_digest")
    return canonical.encode()


def goal_has_blocking_review(db, goal_id: UUID) -> bool:
    """最新 GoalReview 含 BLOCKER 时返回 True；无诊断行则 False。

    findings 仅作规则门控：不改 Goal 状态、不贡献 criterion PASS、不写 DONE。
    """
    row = (
        db.execute(
            text(
                """SELECT findings FROM goal_reviews
                WHERE goal_id=:goal ORDER BY created_at DESC, id DESC LIMIT 1"""
            ),
            {"goal": goal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return False
    findings = row["findings"] or []
    return any(
        isinstance(item, dict) and item.get("severity") == "BLOCKER" for item in findings
    )


def _goal_review_content_from_row(row) -> dict:
    """复现落库时用于 Content v3 digest 的 content（findings 保序，evidence_ids 集合排序）。"""
    findings_payload = []
    for finding in row["findings"] or []:
        item = dict(finding)
        item["evidence_ids"] = sorted(str(i) for i in item.get("evidence_ids") or [])
        findings_payload.append(item)
    return {
        "producer_activity_id": str(row["activity_id"]),
        "producer_attempt_id": str(row["attempt_id"]),
        "goal_id": str(row["goal_id"]),
        "goal_contract_revision": int(row["goal_contract_revision"]),
        "plan_revision": row["plan_revision"],
        "review_snapshot_digest": row["review_snapshot_digest"],
        "findings": findings_payload,
    }


def goal_review_payloads_for_planner(
    db,
    goal_id: UUID,
    *,
    goal_contract_revision: int | None,
    plan_revision: int | None,
    limit: int = 20,
) -> list[tuple[str, bytes]]:
    """适用版本的 GoalReview → (content_digest, canonical_bytes)；仅供 PLANNER 绑定。

    禁止写入 planning_feedback；以 EVIDENCE 工件进入 ContextBundle.input_bindings。
    """
    if goal_contract_revision is None:
        return []
    rows = (
        db.execute(
            text(
                """SELECT * FROM goal_reviews
                WHERE goal_id=:goal
                  AND goal_contract_revision=:grev
                  AND plan_revision IS NOT DISTINCT FROM :prev
                ORDER BY created_at DESC, id DESC
                LIMIT :limit"""
            ),
            {
                "goal": goal_id,
                "grev": goal_contract_revision,
                "prev": plan_revision,
                "limit": limit,
            },
        )
        .mappings()
        .all()
    )
    out: list[tuple[str, bytes]] = []
    for row in rows:
        content = _goal_review_content_from_row(row)
        from evidence_ledger.content import encode

        payload = encode(
            json.dumps(
                {
                    "schema_version": 3,
                    "object_type": "GoalReview",
                    "content": content,
                    "reference_bindings": [],
                }
            )
        )
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if digest != row["content_digest"]:
            raise PlanRejected(
                f"GoalReview 无法复现 content_digest: {row['id']}"
            )
        out.append((digest, payload))
    # 编译绑定按 artifact 稳定序；这里先按 digest 升序，避免同批抖动
    out.sort(key=lambda item: item[0])
    return out


def _goal_review_budget_limits(contract: dict) -> tuple[int, int, int]:
    """返回 (max_reviews, min_interval_seconds, stagnation_seconds)。

    最大复盘次数复用合同 ``max_plan_revisions``（与重规划同属复盘预算族）。
    最小间隔 / 停滞阈值由墙钟均分，失败关闭防无限 tick。
    """
    retry = contract.get("retry_policy") or {}
    budget = contract.get("budget") or {}
    max_reviews = int(retry.get("max_plan_revisions") or 10)
    wall = int(budget.get("wall_clock_seconds") or 3600)
    if max_reviews < 1:
        raise PlanRejected(
            "max_plan_revisions 无效，拒绝 GOAL_REVIEW",
            code="GOAL_REVIEW_CONTRACT_INVALID",
        )
    if wall < 1:
        raise PlanRejected(
            "wall_clock_seconds 无效，拒绝 GOAL_REVIEW",
            code="GOAL_REVIEW_CONTRACT_INVALID",
        )
    # 墙钟内均匀复盘；下限 1s（小预算测）、上限 600s
    min_interval = min(600, max(1, wall // max(max_reviews * 2, 1)))
    # 停滞阈值 ≥ 最小间隔，且不超过 900s
    stagnation = min(900, max(min_interval, min_interval * 2))
    return max_reviews, min_interval, stagnation


# 与 packages/orchestration temporal_workflows._GOAL_REVIEW_MIN_INTERVAL_SECONDS 对齐
_WORKFLOW_GOAL_REVIEW_MIN_INTERVAL_SECONDS = 30


def goal_review_workflow_start_knobs(contract: dict) -> dict[str, int]:
    """从 Goal 合同派生 Workflow 启动旋钮（与预算快照同源）。

    供 ``ensure_workflow`` / ``build_goal_workflow_payload`` 透传：
    ``goal_review_interval_seconds``、``max_goal_reviews``。
    interval 抬到编排侧下限（30s），否则 Workflow 会失败关闭为零触发，
    出现「预算快照可复盘、工作流永不启」的假接通。
    """
    max_reviews, min_interval, _stagnation = _goal_review_budget_limits(contract)
    return {
        "goal_review_interval_seconds": max(
            _WORKFLOW_GOAL_REVIEW_MIN_INTERVAL_SECONDS, min_interval
        ),
        "max_goal_reviews": max_reviews,
    }


def read_goal_review_workflow_start_knobs(engine, goal_id: UUID) -> dict[str, int]:
    """读 Goal 合同并派生 Workflow Critic 启动旋钮；Goal 不存在则 ScopeNotFound。"""
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        contract = row["contract"]
        if isinstance(contract, str):
            contract = json.loads(contract)
        return goal_review_workflow_start_knobs(dict(contract or {}))


def _count_goal_review_slots(db, goal_id: UUID) -> int:
    """已占用复盘槽：非 CANCELLED 的 GOAL_REVIEW Activity。"""
    return int(
        db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND kind='AUDIT' AND target_type='GOAL_REVIEW'
                  AND status <> 'CANCELLED'"""
            ),
            {"goal": goal_id},
        ).scalar_one()
    )


def _latest_goal_review_created_at(db, goal_id: UUID):
    return db.execute(
        text(
            """SELECT created_at FROM activities
            WHERE goal_id=:goal AND kind='AUDIT' AND target_type='GOAL_REVIEW'
              AND status <> 'CANCELLED'
            ORDER BY created_at DESC, id DESC LIMIT 1"""
        ),
        {"goal": goal_id},
    ).scalar()


def _latest_engineering_progress_at(db, goal_id: UUID):
    """最近一次非 GOAL_REVIEW 工程进展时间（RUNNING/SUCCEEDED）。

    无此类活动时返回 None（允许规划期首次复盘）。
    """
    return db.execute(
        text(
            """SELECT max(updated_at) FROM activities
            WHERE goal_id=:goal
              AND kind IN ('PLAN','EXECUTE','INTEGRATE','AUDIT')
              AND (target_type IS NULL OR target_type <> 'GOAL_REVIEW')
              AND status IN ('RUNNING','SUCCEEDED')"""
        ),
        {"goal": goal_id},
    ).scalar()


def goal_review_budget_snapshot(db, goal_id: UUID, *, now: datetime | None = None) -> dict:
    """只读复盘预算投影；不创建 Activity、不写 Goal DONE。"""
    from datetime import UTC

    goal = (
        db.execute(
            text("SELECT status, contract FROM goals WHERE id=:id"),
            {"id": goal_id},
        )
        .mappings()
        .first()
    )
    if goal is None:
        raise ScopeNotFound()
    max_reviews, min_interval, stagnation = _goal_review_budget_limits(goal["contract"])
    used = _count_goal_review_slots(db, goal_id)
    remaining = max(0, max_reviews - used)
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)

    last_review = _latest_goal_review_created_at(db, goal_id)
    since_review = None
    if last_review is not None:
        lr = last_review if last_review.tzinfo else last_review.replace(tzinfo=UTC)
        since_review = max(0, int((clock - lr).total_seconds()))

    progress_at = _latest_engineering_progress_at(db, goal_id)
    since_progress = None
    if progress_at is not None:
        pr = progress_at if progress_at.tzinfo else progress_at.replace(tzinfo=UTC)
        since_progress = max(0, int((clock - pr).total_seconds()))

    blocking: str | None = None
    if goal["status"] in ("DONE", "CANCELLED", "FAILED"):
        blocking = "GOAL_REVIEW_GOAL_TERMINAL"
    elif used >= max_reviews:
        blocking = "GOAL_REVIEW_BUDGET_EXHAUSTED"
    elif last_review is not None and (since_review or 0) < min_interval:
        blocking = "GOAL_REVIEW_INTERVAL_TOO_SHORT"
    elif progress_at is not None and (since_progress or 0) < stagnation:
        blocking = "GOAL_REVIEW_NOT_STAGNANT"

    return {
        "max_reviews": max_reviews,
        "reviews_used": used,
        "reviews_remaining": remaining,
        "min_interval_seconds": min_interval,
        "stagnation_seconds": stagnation,
        "seconds_since_last_review": since_review,
        "seconds_since_engineering_progress": since_progress,
        "eligible_now": blocking is None,
        "blocking_reason_code": blocking,
        "marks_goal_done": False,
    }


def ensure_goal_review_activity(
    engine: Engine,
    *,
    goal_id: UUID,
    trigger_key: str,
    review_seq: int | None = None,
) -> tuple[UUID, str, bool, int, int, int, int]:
    """按触发键去重创建 READY AUDIT(target=GOAL_REVIEW)；快照 digest 由 Kernel 钉扎。

    返回 (activity_id, review_snapshot_digest, created, reviews_remaining,
    max_reviews, min_interval_seconds, stagnation_seconds)。
    新建时校验最大复盘次数、最小间隔与停滞阈值；超预算失败关闭。不写 Goal/Task DONE。
    """
    if not trigger_key or len(trigger_key) > 512:
        raise PlanRejected("trigger_key 无效", code="GOAL_REVIEW_TRIGGER_INVALID")
    if review_seq is not None and int(review_seq) < 1:
        raise PlanRejected("review_seq 无效", code="GOAL_REVIEW_TRIGGER_INVALID")
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
        if goal["status"] in ("DONE", "CANCELLED", "FAILED"):
            raise PlanRejected(
                "Goal 已终态，不能创建 GOAL_REVIEW",
                code="GOAL_REVIEW_GOAL_TERMINAL",
            )
        max_reviews, min_interval, stagnation = _goal_review_budget_limits(
            goal["contract"]
        )
        review_snapshot_digest, snapshot_payload = build_review_snapshot(db, goal)
        persist_review_snapshot(
            db,
            project_id=goal["project_id"],
            goal_id=goal_id,
            digest=review_snapshot_digest,
            payload=snapshot_payload,
        )
        # 同触发键只生成一份（含在途与已成功）
        probe = json.dumps([{"trigger_key": trigger_key}])
        existing = (
            db.execute(
                text(
                    """SELECT id, binding FROM activities
                WHERE goal_id=:goal AND kind='AUDIT' AND target_type='GOAL_REVIEW'
                  AND verification_assignments @> CAST(:probe AS jsonb)
                  AND status NOT IN ('FAILED','CANCELLED')
                ORDER BY created_at,id LIMIT 1"""
                ),
                {"goal": goal_id, "probe": probe},
            )
            .mappings()
            .first()
        )
        used = _count_goal_review_slots(db, goal_id)
        if existing is not None:
            binding = existing["binding"] or {}
            remaining = max(0, max_reviews - used)
            return (
                existing["id"],
                binding.get("subject_digest") or review_snapshot_digest,
                False,
                remaining,
                max_reviews,
                min_interval,
                stagnation,
            )

        if used >= max_reviews:
            raise PlanRejected(
                f"GOAL_REVIEW 已达上限 {max_reviews}（max_plan_revisions），拒绝无限复盘",
                code="GOAL_REVIEW_BUDGET_EXHAUSTED",
            )
        latest_at = _latest_goal_review_created_at(db, goal_id)
        if latest_at is not None:
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            created = latest_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            elapsed = (now - created).total_seconds()
            if elapsed < min_interval:
                raise PlanRejected(
                    f"GOAL_REVIEW 间隔过短（需 ≥{min_interval}s，已过 {int(elapsed)}s）",
                    code="GOAL_REVIEW_INTERVAL_TOO_SHORT",
                )

        # 停滞门：若存在近期 RUNNING/SUCCEEDED 工程活动，拒绝复盘（Critic 只看卡住）
        progress_at = _latest_engineering_progress_at(db, goal_id)
        if progress_at is not None:
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            progressed = progress_at
            if progressed.tzinfo is None:
                progressed = progressed.replace(tzinfo=UTC)
            idle = (now - progressed).total_seconds()
            if idle < stagnation:
                raise PlanRejected(
                    f"工程尚未停滞（需 ≥{stagnation}s 无进展，已空闲 {int(idle)}s），拒绝 GOAL_REVIEW",
                    code="GOAL_REVIEW_NOT_STAGNANT",
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
        binding = ExecutionBinding(
            goal_contract_revision=goal["contract_revision"],
            goal_contract_digest=goal["contract_digest"],
            task_contract_revision=None,
            task_contract_digest=None,
            plan_revision=goal["plan_revision"],
            subject_digest=review_snapshot_digest,
            policy_digest=policy_digest,
            model_profile_digest=model_digest,
            skill_set_digest=skill_digest,
        )
        assignment: dict = {
            "trigger_key": trigger_key,
            "review_snapshot_digest": review_snapshot_digest,
        }
        if review_seq is not None:
            assignment["review_seq"] = int(review_seq)
        assignments = [assignment]
        activity_id = uuid4()
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,NULL,:goal,'AUDIT','GOAL_REVIEW',:goal,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'READY',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": activity_id,
                "project": goal["project_id"],
                "goal": goal_id,
                "binding": binding.model_dump_json(),
                "assignments": json.dumps(assignments),
                "resources": _GOAL_REVIEW_RESOURCES.model_dump_json(),
            },
        )
        remaining = max(0, max_reviews - (used + 1))
        return (
            activity_id,
            review_snapshot_digest,
            True,
            remaining,
            max_reviews,
            min_interval,
            stagnation,
        )


def request_goal_review(
    engine: Engine,
    goal_id: UUID,
    *,
    trigger_key: str,
    review_seq: int | None = None,
) -> dict:
    """编排侧调用入口：去重 ensure + 预算门禁；恒 ``marks_goal_done=False``。

    拒绝时不抛异常，回传 ``ok=False`` + ``reason_code``（失败关闭、可调度退避）。
    """
    try:
        (
            activity_id,
            digest,
            created,
            remaining,
            max_reviews,
            min_interval,
            stagnation,
        ) = ensure_goal_review_activity(
            engine,
            goal_id=goal_id,
            trigger_key=trigger_key,
            review_seq=review_seq,
        )
    except PlanRejected as exc:
        return {
            "ok": False,
            "created": False,
            "reason_code": exc.code or "GOAL_REVIEW_REJECTED",
            "message": exc.message,
            "marks_goal_done": False,
        }
    return {
        "ok": True,
        "created": created,
        "activity_id": str(activity_id),
        "review_snapshot_digest": digest,
        "reviews_remaining": remaining,
        "max_reviews": max_reviews,
        "min_interval_seconds": min_interval,
        "stagnation_seconds": stagnation,
        "marks_goal_done": False,
    }


def submit_goal_review_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    """落库 GoalReview；Activity SUCCEEDED；绝不推进 Task/Goal DONE。"""
    if not isinstance(body.outcome, AuditGoalReviewOutcome):
        raise PlanRejected("GOAL_REVIEW outcome 形状无效")
    review = body.outcome.review
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
        if body.lease.activity_id != activity_id:
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
        if activity["kind"] != "AUDIT":
            raise PlanRejected("非 AUDIT 活动")
        if activity["target_type"] != "GOAL_REVIEW":
            raise PlanRejected("非 GOAL_REVIEW 目标")
        if activity["target_id"] != activity["goal_id"]:
            raise PlanRejected("GOAL_REVIEW target 必须绑定 Goal")
        if activity["task_id"] is not None:
            raise PlanRejected("GOAL_REVIEW 不得绑定 Task")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if activity["goal_id"] is not None:
            from .stops import assert_goal_allows_success_outcome

            assert_goal_allows_success_outcome(db, activity["goal_id"])
        if activity["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
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

        binding = activity["binding"] or {}
        if review.goal_contract_revision != binding.get("goal_contract_revision"):
            raise PlanRejected("goal_contract_revision 与 binding 不一致")
        if review.plan_revision != binding.get("plan_revision"):
            raise PlanRejected("plan_revision 与 binding 不一致")
        if review.review_snapshot_digest != binding.get("subject_digest"):
            raise PlanRejected("review_snapshot_digest 与 binding 不一致")
        assignments = activity["verification_assignments"] or []
        if assignments:
            expected_snap = assignments[0].get("review_snapshot_digest")
            if expected_snap and review.review_snapshot_digest != expected_snap:
                raise PlanRejected("review_snapshot_digest 与活动指派不一致")

        findings_payload = []
        for finding in review.findings:
            item = finding.model_dump(mode="json")
            item["evidence_ids"] = sorted(str(i) for i in finding.evidence_ids)
            findings_payload.append(item)
        content = {
            "producer_activity_id": str(activity_id),
            "producer_attempt_id": str(attempt["id"]),
            "goal_id": str(activity["goal_id"]),
            "goal_contract_revision": review.goal_contract_revision,
            "plan_revision": review.plan_revision,
            "review_snapshot_digest": review.review_snapshot_digest,
            "findings": findings_payload,
        }
        digest = _content_digest("GoalReview", content)
        review_id = uuid4()
        db.execute(
            text(
                """INSERT INTO goal_reviews(
                  id,project_id,goal_id,activity_id,attempt_id,
                  goal_contract_revision,plan_revision,review_snapshot_digest,
                  findings,content_digest)
                VALUES(
                  :id,:project,:goal,:activity,:attempt,
                  :grev,:prev,:snap,CAST(:findings AS jsonb),:digest)"""
            ),
            {
                "id": review_id,
                "project": activity["project_id"],
                "goal": activity["goal_id"],
                "activity": activity_id,
                "attempt": attempt["id"],
                "grev": review.goal_contract_revision,
                "prev": review.plan_revision,
                "snap": review.review_snapshot_digest,
                "findings": json.dumps(
                    [f.model_dump(mode="json") for f in review.findings]
                ),
                "digest": digest,
            },
        )
        db.execute(
            text(
                """UPDATE activity_attempts SET status='COMPLETED', finished_at=:now,
              updated_at=clock_timestamp() WHERE id=:id"""
            ),
            {"id": attempt["id"], "now": now},
        )
        db.execute(
            text(
                """UPDATE resource_reservations SET status='RELEASED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""
            ),
            {"id": attempt["id"]},
        )
        db.execute(
            text(
                """UPDATE budget_reservations SET status='RELEASED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""
            ),
            {"id": attempt["id"]},
        )
        updated = (
            db.execute(
                text(
                    """UPDATE activities SET status='SUCCEEDED',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id RETURNING *"""
                ),
                {"id": activity_id},
            )
            .mappings()
            .one()
        )
        # 明确：findings 仅诊断；不写 Task DONE、不写 Goal DONE、不贡献 criterion PASS。
        # 后继无 BLOCKER 时尝试 DRAINING→SEALED（与 Stop/对账对称；≠DONE）
        if activity["goal_id"] is not None:
            from .finalization import try_seal_draining_barrier

            try_seal_draining_barrier(db, activity["goal_id"])
        return _activity_from_row(updated)


def _audit_from_row(row) -> AuditResource:
    return AuditResource.model_validate(
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "project_id": row["project_id"],
            "goal_id": row["goal_id"],
            "task_id": row["task_id"],
            "producer_activity_id": row["activity_id"],
            "producer_attempt_id": row["attempt_id"],
            "subject_candidate_manifest_id": row["subject_candidate_manifest_id"],
            "goal_contract_revision": row["goal_contract_revision"],
            "task_contract_revision": row["task_contract_revision"],
            "verification_profile_id": row["verification_profile_id"],
            "layer": row["layer"],
            "audit_round": row["audit_round"],
            "verifier_run_ids": list(row["verifier_run_ids"] or []),
            "verdict": row["verdict"],
            "criterion_results": row["criterion_results"],
            "evidence_ids": list(row["evidence_ids"] or []),
            "reason": row["reason"],
            "content_digest": row["content_digest"],
        }
    )


def submit_audit_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
) -> ActivityResource:
    if isinstance(body.outcome, AuditGoalReviewOutcome):
        return submit_goal_review_outcome(engine, subject, activity_id, body)
    if not isinstance(body.outcome, AuditCandidateOutcome):
        raise PlanRejected("AUDIT outcome 形状无效")
    audit = body.outcome.audit
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
        if body.lease.activity_id != activity_id:
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
        if activity["kind"] != "AUDIT":
            raise PlanRejected("非 AUDIT 活动")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
        if activity["goal_id"] is not None:
            from .stops import assert_goal_allows_success_outcome

            assert_goal_allows_success_outcome(db, activity["goal_id"])
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

        assignments = activity["verification_assignments"] or []
        if not assignments:
            raise PlanRejected("活动缺少 verification_assignments")
        assignment = assignments[0]
        if str(audit.verification_profile_id) != str(assignment["verification_profile_id"]):
            raise PlanRejected("verification_profile 与分配不一致")
        if audit.layer != assignment.get("layer"):
            raise PlanRejected("layer 与分配不一致")
        if int(audit.audit_round) != int(assignment.get("audit_round", 0)):
            raise PlanRejected("audit_round 与分配不一致")
        if audit.subject_candidate_manifest_id != activity["target_id"]:
            raise PlanRejected("候选与活动 target 不一致")
        binding = activity["binding"] or {}
        if audit.goal_contract_revision != binding.get("goal_contract_revision"):
            raise PlanRejected("goal_contract_revision 与 binding 不一致")
        if audit.task_contract_revision != binding.get("task_contract_revision"):
            raise PlanRejected("task_contract_revision 与 binding 不一致")

        # 严格：每个 verifier_run_id 须存在且与本活动 lease/assignment/subject 对齐。
        require_matching_verifier_runs(
            db,
            run_ids=list(audit.verifier_run_ids),
            producer_activity_id=activity_id,
            producer_attempt_id=attempt["id"],
            subject_type="CANDIDATE",
            subject_id=audit.subject_candidate_manifest_id,
            verification_profile_id=audit.verification_profile_id,
            layer=str(audit.layer),
            audit_round=int(audit.audit_round),
        )
        # verdict 必须与逐项结果一致，禁止空数组冒充 PASS。
        item_verdicts = {c.criterion_id: c.verdict for c in audit.criterion_results}
        if audit.verdict == "PASS" and any(v != "PASS" for v in item_verdicts.values()):
            raise PlanRejected("AUDIT_VERDICT_MISMATCH")
        if audit.verdict == "FAIL" and all(v == "PASS" for v in item_verdicts.values()):
            raise PlanRejected("AUDIT_VERDICT_MISMATCH")
        if not audit.criterion_results:
            raise PlanRejected("criterion_results 不能为空")

        content = {
            "producer_activity_id": str(activity_id),
            "producer_attempt_id": str(attempt["id"]),
            "subject_candidate_manifest_id": str(audit.subject_candidate_manifest_id),
            "goal_contract_revision": audit.goal_contract_revision,
            "task_contract_revision": audit.task_contract_revision,
            "verification_profile_id": str(audit.verification_profile_id),
            "layer": audit.layer,
            "audit_round": audit.audit_round,
            "verifier_run_ids": sorted(str(i) for i in audit.verifier_run_ids),
            "verdict": audit.verdict,
            "criterion_results": sorted(
                (c.model_dump(mode="json") for c in audit.criterion_results),
                key=lambda item: item["criterion_id"],
            ),
            "evidence_ids": sorted(str(i) for i in audit.evidence_ids),
            "reason": audit.reason,
        }
        digest = _content_digest("Audit", content)

        db.execute(
            text("""INSERT INTO audits(
              id,project_id,goal_id,task_id,activity_id,attempt_id,
              subject_candidate_manifest_id,goal_contract_revision,task_contract_revision,
              verification_profile_id,layer,audit_round,verifier_run_ids,verdict,
              criterion_results,evidence_ids,reason,content_digest)
            VALUES(
              :id,:project,:goal,:task,:activity,:attempt,
              :candidate,:grev,:trev,
              :profile,:layer,:round,:runs,:verdict,
              CAST(:results AS jsonb),:evidence,:reason,:digest)"""),
            {
                "id": uuid4(),
                "project": activity["project_id"],
                "goal": activity["goal_id"],
                "task": activity["task_id"],
                "activity": activity_id,
                "attempt": attempt["id"],
                "candidate": audit.subject_candidate_manifest_id,
                "grev": audit.goal_contract_revision,
                "trev": audit.task_contract_revision,
                "profile": audit.verification_profile_id,
                "layer": audit.layer,
                "round": audit.audit_round,
                "runs": audit.verifier_run_ids,
                "verdict": audit.verdict,
                "results": json.dumps([c.model_dump(mode="json") for c in audit.criterion_results]),
                "evidence": audit.evidence_ids,
                "reason": audit.reason,
                "digest": digest,
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

        # Task 完成判定：该 Task 全部 AUDIT Activity 已 SUCCEEDED，且对应 audits 均为 PASS。
        if activity["task_id"] is not None:
            from .feedback import maybe_create_planning_feedback

            maybe_create_planning_feedback(
                db,
                project_id=activity["project_id"],
                goal_id=activity["goal_id"],
                task_id=activity["task_id"],
                candidate_id=activity["target_id"],
            )
            open_audit = db.execute(
                text("""SELECT 1 FROM activities
                WHERE task_id=:task AND kind='AUDIT'
                  AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')
                LIMIT 1"""),
                {"task": activity["task_id"]},
            ).first()
            fail_or_insuff = db.execute(
                text("""SELECT 1 FROM audits
                WHERE task_id=:task AND verdict <> 'PASS'
                LIMIT 1"""),
                {"task": activity["task_id"]},
            ).first()
            if open_audit is None and fail_or_insuff is None:
                from .obligations import assert_no_pending_obligations

                # Task DONE 前排空候选上的 OPEN/QUARANTINED 义务（本轮应已 ASSESSED）
                assert_no_pending_obligations(
                    db, subject_id=activity["target_id"]
                )
                db.execute(
                    text("""UPDATE tasks SET status='DONE',
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id AND status='VERIFYING'"""),
                    {"id": activity["task_id"]},
                )
                from .integrate import maybe_schedule_integrate

                maybe_schedule_integrate(db, activity["goal_id"])
        # 明确：Task DONE 不等于 Goal DONE；Goal DONE 仅经 INTEGRATE + 最终屏障。
        return _activity_from_row(updated)


def list_task_audits(
    engine: Engine,
    task_id: UUID,
    project_ids: list[str],
    *,
    limit: int | None = None,
    after: UUID | None = None,
) -> list[AuditResource]:
    with engine.connect() as db:
        task = (
            db.execute(text("SELECT * FROM tasks WHERE id=:id"), {"id": task_id})
            .mappings()
            .first()
        )
        if task is None:
            raise ScopeNotFound()
        if project_ids and str(task["project_id"]) not in project_ids:
            raise ScopeNotFound()
        # 兼容旧调用：未传 limit 则全量（测试与内部只读）
        if limit is None:
            rows = (
                db.execute(
                    text(
                        """SELECT * FROM audits WHERE task_id=:task
                        ORDER BY created_at, id"""
                    ),
                    {"task": task_id},
                )
                .mappings()
                .all()
            )
            return [_audit_from_row(r) for r in rows]
        rows = (
            db.execute(
                text(
                    """SELECT * FROM audits WHERE task_id=:task
                    AND (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
                      SELECT created_at,id FROM audits WHERE id=CAST(:after AS uuid)))
                    ORDER BY created_at, id LIMIT :limit"""
                ),
                {"task": task_id, "after": after, "limit": limit},
            )
            .mappings()
            .all()
        )
        return [_audit_from_row(r) for r in rows]


def _required_slots(db, task_id: UUID | None) -> list[tuple[str, str, str]]:
    """返回 (profile_id, layer, criterion_id) 必需槽；无 Task 时为空。"""
    if task_id is None:
        return []
    task = (
        db.execute(text("SELECT contract FROM tasks WHERE id=:id"), {"id": task_id})
        .mappings()
        .first()
    )
    if task is None:
        return []
    contract = task["contract"]
    if isinstance(contract, str):
        contract = json.loads(contract)
    slots: list[tuple[str, str, str]] = []
    for acceptance in contract.get("acceptance") or []:
        if not acceptance.get("required", True):
            continue
        profile_id = str(acceptance["verification_profile_id"])
        profile = (
            db.execute(
                text("SELECT config FROM verification_profiles WHERE id=:id"),
                {"id": profile_id},
            )
            .mappings()
            .first()
        )
        layers = list((profile["config"] if profile else {}).get("required_layers") or ["MECHANICAL"])
        for layer in layers:
            slots.append((profile_id, layer, str(acceptance["id"])))
    return slots


def _aggregate_candidate(
    db,
    *,
    goal: dict,
    candidate_id: UUID,
    trust_revision: str,
) -> AuditAggregation:
    rows = (
        db.execute(
            text(
                """SELECT * FROM audits
                WHERE goal_id=:goal AND subject_candidate_manifest_id=:candidate
                ORDER BY created_at, id"""
            ),
            {"goal": goal["id"], "candidate": candidate_id},
        )
        .mappings()
        .all()
    )
    task_id = rows[0]["task_id"] if rows else None
    required = _required_slots(db, task_id)
    required_sorted = sorted(required)
    required_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(required_sorted, separators=(",", ":")).encode()
        ).hexdigest()
    )
    covered: set[tuple[str, str, str]] = set()
    accepted: list[UUID] = []
    blocking: list[str] = []
    has_fail = False
    has_insuff = False
    for row in rows:
        accepted.append(row["id"])
        if row["verdict"] == "FAIL":
            has_fail = True
            blocking.append("AUDIT_FAIL")
        elif row["verdict"] == "INSUFFICIENT":
            has_insuff = True
            blocking.append("AUDIT_INSUFFICIENT")
        elif row["verdict"] == "PASS":
            for crit in row["criterion_results"] or []:
                if crit.get("verdict") == "PASS":
                    covered.add(
                        (
                            str(row["verification_profile_id"]),
                            str(row["layer"]),
                            str(crit["criterion_id"]),
                        )
                    )
    missing = [
        MissingAuditItem(
            verification_profile_id=UUID(profile),
            layer=layer,  # type: ignore[arg-type]
            criterion_id=criterion,
        )
        for profile, layer, criterion in required_sorted
        if (profile, layer, criterion) not in covered
    ]
    if has_fail:
        verdict = "FAIL"
    elif missing or has_insuff:
        verdict = "INSUFFICIENT"
        if missing and "MISSING_REQUIRED" not in blocking:
            blocking.append("MISSING_REQUIRED")
    else:
        verdict = "PASS"
        blocking = []
    goal_rev = rows[0]["goal_contract_revision"] if rows else goal["contract_revision"]
    task_rev = rows[0]["task_contract_revision"] if rows else None
    # 回填已知 Assessment id；不替代 Audit 判定 DONE（DONE 仍只走 audits 全集 PASS）
    assessment_ids = [
        row["id"]
        for row in db.execute(
            text(
                """SELECT id FROM verification_assessments
                WHERE project_id=:project AND subject_type='CANDIDATE'
                  AND subject_id=:candidate
                ORDER BY created_at, id"""
            ),
            {"project": goal["project_id"], "candidate": candidate_id},
        ).mappings()
    ]
    return AuditAggregation(
        subject_candidate_manifest_id=candidate_id,
        goal_contract_revision=goal_rev,
        task_contract_revision=task_rev,
        plan_revision=goal["plan_revision"] or 1,
        required_set_digest=required_digest,
        accepted_audit_ids=accepted,
        accepted_assessment_ids=assessment_ids,
        pending_verification_count=0,
        trust_revision=trust_revision,
        verdict=verdict,  # type: ignore[arg-type]
        blocking_reason_codes=sorted(set(blocking)),
        missing_items=missing,
    )


def list_goal_audits(
    engine: Engine,
    goal_id: UUID,
    subject: str,
    project_ids: list[str],
    limit: int,
    after: UUID | None,
) -> list[GoalAuditItem]:
    """按 Goal 列出候选验收与周期诊断（CANDIDATE_AUDIT | GOAL_REVIEW）。"""
    with engine.connect() as db:
        from .policies import ConfigurationVersions

        goal = (
            db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": goal_id})
            .mappings()
            .first()
        )
        if goal is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, goal["project_id"], subject, project_ids)
        trust_revision = str(
            db.execute(
                text("SELECT trust_revision FROM project_trust_states WHERE project_id=:id"),
                {"id": goal["project_id"]},
            ).scalar_one()
        )
        # 联合游标：audits ∪ goal_reviews，按 (created_at,id) 稳定分页
        page = db.execute(
            text(
                """WITH items AS (
              SELECT id, created_at, 'CANDIDATE_AUDIT'::text AS record_type
                FROM audits WHERE goal_id=:goal
              UNION ALL
              SELECT id, created_at, 'GOAL_REVIEW'::text AS record_type
                FROM goal_reviews WHERE goal_id=:goal
            )
            SELECT * FROM items
            WHERE (CAST(:after AS uuid) IS NULL OR (created_at,id)>(
              SELECT created_at,id FROM items WHERE id=CAST(:after AS uuid)))
            ORDER BY created_at,id LIMIT :limit"""
            ),
            {"goal": goal_id, "after": after, "limit": limit},
        ).mappings()
        cache: dict[UUID, AuditAggregation] = {}
        items: list[GoalAuditItem] = []
        for ref in page:
            if ref["record_type"] == "GOAL_REVIEW":
                row = (
                    db.execute(
                        text("SELECT * FROM goal_reviews WHERE id=:id"),
                        {"id": ref["id"]},
                    )
                    .mappings()
                    .one()
                )
                items.append(GoalReviewItem(review=_goal_review_from_row(row)))
                continue
            row = (
                db.execute(text("SELECT * FROM audits WHERE id=:id"), {"id": ref["id"]})
                .mappings()
                .one()
            )
            audit = _audit_from_row(row)
            candidate = audit.subject_candidate_manifest_id
            if candidate not in cache:
                cache[candidate] = _aggregate_candidate(
                    db,
                    goal=goal,
                    candidate_id=candidate,
                    trust_revision=trust_revision,
                )
            items.append(
                CandidateAuditItem(audit=audit, aggregation=cache[candidate])
            )
        return items
