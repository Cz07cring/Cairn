"""Claim READY Activity 与 heartbeat；含失租扫描，无工具路径。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.runtime import (
    ActivityAttemptResource,
    ActivityLease,
    BindingStale,
    ClaimRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseIdentity,
    LeaseRejected,
    WorkerForbidden,
)
from .activities import _activity_from_row
from .goals import legacy_claim_drain_allowed, temporal_orchestration_enforced
from .policies import ConfigurationVersions, ScopeNotFound
from .projects import ProjectConflict

DEFAULT_LEASE_TTL = timedelta(seconds=90)


def _pending_stop_ids_for_attempt(db, attempt_id: UUID) -> list[UUID]:
    """当前 attempt 上仍 REQUESTED 的 Stop（供 heartbeat.control=STOP）。"""
    rows = db.execute(
        text(
            """SELECT id FROM stops
            WHERE attempt_id=:id AND status='REQUESTED'
            ORDER BY created_at, id"""
        ),
        {"id": attempt_id},
    ).scalars()
    return list(rows)


_GOAL_DRAIN_HEARTBEAT_STOP = frozenset(
    {"PAUSING", "PAUSED", "CANCELLING", "CANCELLED", "BLOCKED"}
)


def _heartbeat_control_for_attempt(
    db, *, activity_id: UUID, attempt_id: UUID
) -> tuple[str, list[UUID]]:
    """有 REQUESTED Stop，或 Goal 处于暂停/取消/BLOCKED → STOP（禁止重开工具）。"""
    pending = _pending_stop_ids_for_attempt(db, attempt_id)
    if pending:
        return "STOP", pending
    goal_status = db.execute(
        text(
            """SELECT g.status FROM goals g
            JOIN activities a ON a.goal_id = g.id
            WHERE a.id=:id"""
        ),
        {"id": activity_id},
    ).scalar()
    if goal_status in _GOAL_DRAIN_HEARTBEAT_STOP:
        return "STOP", []
    return "CONTINUE", []


def expire_stale_leases(db) -> int:
    """将过期 ACTIVE attempt 标为 EXPIRED，Activity RUNNING→RECOVERING→READY。

    失租时先发出 reason=LEASE_EXPIRED 的 StopRequest（仅 REQUESTED），再标 EXPIRED。
    资源/预算 HELD→QUARANTINED（禁止失租即 RELEASED）；待 StopReceipt
    EXITED∧compute_released 确认后再释放。不伪造 CONFIRMED、不杀进程；
    DISPATCHED effect 升格 UNKNOWN；是否 READY 一律经 reassess_recovering_activity
    （须 LEASE_EXPIRED Stop 已 CONFIRMED，禁止「无 effect 即重开」）。

    Issue #22：按 Goal 先取 admission 锁再锁 attempt，与 finalize DONE 互斥；
    Goal 已终态时零新增 Stop/UNKNOWN/隔离事实。
    """
    from ..protocols.runtime import StopRequest
    from .goals import acquire_goal_admission_lock
    from .stops import insert_stop_request

    now = datetime.now(UTC)
    # 不先锁 attempt：先按 goal_id 排序取 admission，再 FOR UPDATE attempt（防死锁）
    stale = (
        db.execute(
            text(
                """SELECT att.id, att.activity_id, att.fencing_epoch, att.project_id,
                          a.goal_id
                FROM activity_attempts att
                JOIN activities a ON a.id = att.activity_id
                WHERE att.status='ACTIVE' AND att.lease_expires_at <= :now
                ORDER BY a.goal_id NULLS FIRST, att.lease_expires_at, att.id"""
            ),
            {"now": now},
        )
        .mappings()
        .all()
    )
    recovered = 0
    deadline = now + timedelta(hours=1)
    for row in stale:
        goal_id = row["goal_id"]
        if goal_id is not None:
            acquire_goal_admission_lock(db, goal_id)
            goal_status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal_id},
            ).scalar()
            if goal_status in ("DONE", "FAILED", "CANCELLED"):
                # 终态后不得再受理 late Stop / UNKNOWN / 隔离写入
                continue

        locked = (
            db.execute(
                text(
                    """SELECT att.id, att.activity_id, att.fencing_epoch, att.project_id
                    FROM activity_attempts att
                    WHERE att.id=:id AND att.status='ACTIVE'
                      AND att.lease_expires_at <= :now
                    FOR UPDATE OF att"""
                ),
                {"id": row["id"], "now": now},
            )
            .mappings()
            .first()
        )
        if locked is None:
            continue

        # 先记账停机意图（activation_id == attempt_id）
        insert_stop_request(
            db,
            StopRequest(
                request_id=uuid4(),
                activation_id=locked["id"],
                activity_id=locked["activity_id"],
                attempt_id=locked["id"],
                fencing_epoch=str(locked["fencing_epoch"]),
                reason="LEASE_EXPIRED",
                deadline_at=deadline,
            ),
            locked["project_id"],
        )
        db.execute(
            text("""UPDATE activity_attempts
              SET status='EXPIRED', finished_at=:now, updated_at=clock_timestamp()
              WHERE id=:id AND status='ACTIVE'"""),
            {"id": locked["id"], "now": now},
        )
        # R03/TR04：失租隔离，不得在未确认停机前释放槽位/预算
        db.execute(
            text("""UPDATE resource_reservations
              SET status='QUARANTINED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""),
            {"id": locked["id"]},
        )
        db.execute(
            text("""UPDATE budget_reservations
              SET status='QUARANTINED', updated_at=clock_timestamp()
              WHERE attempt_id=:id AND status='HELD'"""),
            {"id": locked["id"]},
        )
        recovering = (
            db.execute(
                text("""UPDATE activities
                  SET status='RECOVERING',
                      state_revision=state_revision+1,
                      current_attempt_id=NULL,
                      updated_at=clock_timestamp()
                  WHERE id=:id AND status='RUNNING'
                  RETURNING id, project_id"""),
                {"id": locked["activity_id"]},
            )
            .mappings()
            .first()
        )
        if recovering is None:
            continue
        # 失租后 DISPATCHED 结果不可再经活租约终态；升格 UNKNOWN 供 reconcile（禁盲再 dispatch）
        db.execute(
            text(
                """UPDATE effect_intents
                  SET status='UNKNOWN',
                      state_revision=state_revision+1,
                      updated_at=clock_timestamp()
                  WHERE activity_id=:activity
                    AND producer_attempt_id=:attempt
                    AND status='DISPATCHED'"""
            ),
            {"activity": recovering["id"], "attempt": locked["id"]},
        )
        # AB02：PREPARED/AUTHORIZED 从未发出，无外部副作用——保留原 effect_id 供重领续跑，
        # 不 CANCELLED（UNIQUE(activity,logical_step,rev) 禁止另插一行）。是否 READY 见 reassess。
        # 统一经 reassess：仅 DISPATCHED/UNKNOWN 挡重领；PREPARED 可安全续跑。
        # 注意资源/预算仍保持 QUARANTINED，直到 Stop 收到可信停机回执才释放——
        # lease 到期≠进程已停，故不得失租即释放资源；但这不阻塞活动重领。
        reassess_recovering_activity(db, recovering["id"])
        pending = db.execute(
            text("""SELECT 1 FROM effect_intents
              WHERE activity_id=:activity
                AND status IN ('DISPATCHED','UNKNOWN')
              LIMIT 1"""),
            {"activity": recovering["id"]},
        ).first()
        still_recovering = db.execute(
            text("SELECT 1 FROM activities WHERE id=:id AND status='RECOVERING'"),
            {"id": recovering["id"]},
        ).first()
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'LEASE_EXPIRED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": recovering["project_id"],
                "payload": json.dumps(
                    {
                        "activity_id": str(recovering["id"]),
                        "attempt_id": str(locked["id"]),
                        "held_for_effects": pending is not None,
                        "awaiting_stop_confirm": still_recovering is not None,
                    },
                    separators=(",", ":"),
                ),
            },
        )
        recovered += 1
        if goal_id is not None:
            from .control_commands import maybe_complete_pause_or_cancel

            maybe_complete_pause_or_cancel(db, goal_id)
            task_id = db.execute(
                text("SELECT task_id FROM activities WHERE id=:id"),
                {"id": recovering["id"]},
            ).scalar()
            if task_id is not None:
                from .task_commands import maybe_complete_task_cancel

                maybe_complete_task_cancel(db, task_id)
    return recovered


def reassess_recovering_activity(db, activity_id) -> None:
    """RECOVERING 且无未决 effect → READY（不影响该 attempt 资源仍 QUARANTINED）。

    规格判据（doc/01 状态机）：
    - `RUNNING→RECOVERING`：lease 过期或主动恢复；先隔离旧执行者，保留未决 effect
    - `WAITING/RECOVERING→READY`：**条件恢复且 effect 可安全续跑**；所有权新建，不复用旧 epoch

    因此本函数只按「有无未决 effect」判定可否重领；**不以 Stop 是否 CONFIRMED 为前置**。

    为什么不对 Stop 加门（hermes-c04 裁定，2026-09-12）：
    `stops` 的 CONFIRMED 当且仅当收到 `observation=EXITED ∧ compute_released` 的可信回执
    （storage/stops.py）。未接进程杀伤前，harness 适配器按红线诚实上报 `RUNNING`，
    故 LEASE_EXPIRED 的 Stop 在本机**永不可能** CONFIRMED。若在此处要求 CONFIRMED，
    任何租约过期都会令 Activity 永久停留 RECOVERING、新 worker 永远 409 INVALID_STATE
    —— 活动级死锁（TM04 冻结验收测即测此路径）。

    真正需要「等确认」的是**资源/预算释放**（v0.6/01：旧进程未知时资源保留
    QUARANTINED、费用保持待核对；禁止失租即释放）——该约束在 expire 路径与
    stops 回执路径各自成立，与本函数的重领判定正交，不受本改动影响。

    对账 SUCCEEDED 路径不走这里（由 reconcile 直接采纳 → SUCCEEDED）。
    **仍禁止**在存在 UNKNOWN/DISPATCHED 未决 effect 时重开新 attempt。
    PREPARED/AUTHORIZED（从未 DISPATCHED）视为可安全续跑，不挡 READY（AB02）。
    """
    activity = (
        db.execute(
            text("SELECT id, status FROM activities WHERE id=:id FOR UPDATE"),
            {"id": activity_id},
        )
        .mappings()
        .first()
    )
    if activity is None or activity["status"] != "RECOVERING":
        return
    pending = db.execute(
        text(
            """SELECT 1 FROM effect_intents
              WHERE activity_id=:activity
                AND status IN ('DISPATCHED','UNKNOWN')
              LIMIT 1"""
        ),
        {"activity": activity_id},
    ).first()
    if pending is not None:
        return
    db.execute(
        text(
            """UPDATE activities
              SET status='READY',
                  state_revision=state_revision+1,
                  updated_at=clock_timestamp()
              WHERE id=:id AND status='RECOVERING'"""
        ),
        {"id": activity_id},
    )


def binding_digest_of(binding: dict) -> str:
    canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _attempt_from_row(row) -> ActivityAttemptResource:
    data = dict(row)
    return ActivityAttemptResource.model_validate(
        {
            "id": data["id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "activity_id": data["activity_id"],
            "worker_id": data["worker_id"],
            "binding_digest": data["binding_digest"],
            "fencing_epoch": str(data["fencing_epoch"]),
            "lease_expires_at": data["lease_expires_at"],
            "renewal_seq": data["renewal_seq"],
            "status": data["status"],
            "context_digest": data["context_digest"],
            "model_snapshot": data["model_snapshot"],
            "skill_versions": data["skill_versions"] or [],
            "started_at": data["started_at"],
            "finished_at": data["finished_at"],
        }
    )


def list_attempts_for_activity(
    engine: Engine,
    activity_id: UUID,
    subject: str,
    project_ids: list[str],
    limit: int,
    after: UUID | None,
) -> list[ActivityAttemptResource]:
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
                """SELECT * FROM activity_attempts WHERE activity_id=:activity
            AND (CAST(:after AS uuid) IS NULL OR (started_at,id)>(
              SELECT started_at,id FROM activity_attempts WHERE id=CAST(:after AS uuid)
                AND activity_id=:activity))
            ORDER BY started_at,id LIMIT :limit"""
            ),
            {"activity": activity_id, "after": after, "limit": limit},
        ).mappings()
        return [_attempt_from_row(row) for row in rows]


def _live_binding(db, activity) -> dict:
    if activity["kind"] == "PROBE_MODEL":
        profile = (
            db.execute(
                text("SELECT * FROM model_profiles WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .one()
        )
        policy_digest = activity["binding"]["policy_digest"]
        return {
            "goal_contract_revision": None,
            "goal_contract_digest": None,
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": None,
            "subject_digest": profile["content_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": profile["content_digest"],
            "skill_set_digest": None,
        }
    if activity["kind"] == "VALIDATE_SKILL" and activity["target_type"] == "SKILL_VERSION":
        skill = (
            db.execute(
                text("SELECT content_digest FROM skill_versions WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .one()
        )
        policy_digest = activity["binding"]["policy_digest"]
        return {
            "goal_contract_revision": None,
            "goal_contract_digest": None,
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": None,
            "subject_digest": skill["content_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": None,
            "skill_set_digest": None,
        }
    if activity["kind"] == "INDEX_MEMORY" and activity["target_type"] == "MEMORY_INDEX":
        from .memory_index import memory_ids_digest

        proposed = (
            db.execute(
                text(
                    """SELECT id FROM memories
                    WHERE project_id=:project AND status='PROPOSED'
                    ORDER BY id"""
                ),
                {"project": activity["project_id"]},
            )
            .scalars()
            .all()
        )
        policy_digest = activity["binding"]["policy_digest"]
        return {
            "goal_contract_revision": None,
            "goal_contract_digest": None,
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": None,
            "subject_digest": memory_ids_digest(list(proposed)),
            "policy_digest": policy_digest,
            "model_profile_digest": None,
            "skill_set_digest": None,
        }
    if activity["goal_id"] is None:
        raise BindingStale()
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
    if activity["kind"] == "EXECUTE" and activity["task_id"] is not None:
        task = (
            db.execute(text("SELECT * FROM tasks WHERE id=:id"), {"id": activity["task_id"]})
            .mappings()
            .one()
        )
        return {
            "goal_contract_revision": goal["contract_revision"],
            "goal_contract_digest": goal["contract_digest"],
            "task_contract_revision": task["contract_revision"],
            "task_contract_digest": task["contract_digest"],
            "plan_revision": goal["plan_revision"],
            "subject_digest": task["contract_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": model_digest,
            "skill_set_digest": skill_digest,
        }
    if activity["kind"] == "AUDIT" and activity["target_type"] == "CANDIDATE":
        task = (
            db.execute(text("SELECT * FROM tasks WHERE id=:id"), {"id": activity["task_id"]})
            .mappings()
            .one()
        )
        candidate = (
            db.execute(
                text("SELECT content_digest FROM candidate_manifests WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .one()
        )
        return {
            "goal_contract_revision": goal["contract_revision"],
            "goal_contract_digest": goal["contract_digest"],
            "task_contract_revision": task["contract_revision"],
            "task_contract_digest": task["contract_digest"],
            "plan_revision": goal["plan_revision"],
            "subject_digest": candidate["content_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": model_digest,
            "skill_set_digest": skill_digest,
        }
    if activity["kind"] == "AUDIT" and activity["target_type"] == "GOAL_REVIEW":
        # 快照 digest 钉在活动创建时；合同/计划版本随 live Goal 校验
        assignments = activity["verification_assignments"] or []
        snapshot = None
        if assignments and isinstance(assignments[0], dict):
            snapshot = assignments[0].get("review_snapshot_digest")
        if not snapshot:
            snapshot = (activity["binding"] or {}).get("subject_digest")
        if not snapshot:
            snapshot = goal["contract_digest"]
        return {
            "goal_contract_revision": goal["contract_revision"],
            "goal_contract_digest": goal["contract_digest"],
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": goal["plan_revision"],
            "subject_digest": snapshot,
            "policy_digest": policy_digest,
            "model_profile_digest": model_digest,
            "skill_set_digest": skill_digest,
        }
    if activity["kind"] == "FINALIZE" and activity["target_type"] == "CANDIDATE":
        candidate = (
            db.execute(
                text("SELECT content_digest FROM candidate_manifests WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .one()
        )
        return {
            "goal_contract_revision": goal["contract_revision"],
            "goal_contract_digest": goal["contract_digest"],
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": goal["plan_revision"],
            "subject_digest": candidate["content_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": model_digest,
            "skill_set_digest": skill_digest,
        }
    if activity["kind"] == "INTEGRATE" and activity["target_type"] == "INTEGRATION":
        return {
            "goal_contract_revision": goal["contract_revision"],
            "goal_contract_digest": goal["contract_digest"],
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": goal["plan_revision"],
            "subject_digest": goal["contract_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": model_digest,
            "skill_set_digest": skill_digest,
        }
    if activity["kind"] == "RECONCILE" and activity["target_type"] == "EFFECT":
        effect = (
            db.execute(
                text("SELECT payload_digest FROM effect_intents WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .one()
        )
        return {
            "goal_contract_revision": goal["contract_revision"],
            "goal_contract_digest": goal["contract_digest"],
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": goal["plan_revision"],
            "subject_digest": effect["payload_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": model_digest,
            "skill_set_digest": skill_digest,
        }
    if activity["kind"] == "EXPORT_EVIDENCE" and activity["target_type"] == "RELEASE_EXPORT":
        release = (
            db.execute(
                text("SELECT content_digest FROM release_manifests WHERE id=:id"),
                {"id": activity["target_id"]},
            )
            .mappings()
            .one()
        )
        return {
            "goal_contract_revision": goal["contract_revision"],
            "goal_contract_digest": goal["contract_digest"],
            "task_contract_revision": None,
            "task_contract_digest": None,
            "plan_revision": goal["plan_revision"],
            "subject_digest": release["content_digest"],
            "policy_digest": policy_digest,
            "model_profile_digest": model_digest,
            "skill_set_digest": skill_digest,
        }
    return {
        "goal_contract_revision": goal["contract_revision"],
        "goal_contract_digest": goal["contract_digest"],
        "task_contract_revision": None,
        "task_contract_digest": None,
        "plan_revision": goal["plan_revision"],
        "subject_digest": goal["contract_digest"],
        "policy_digest": policy_digest,
        "model_profile_digest": model_digest,
        "skill_set_digest": skill_digest,
    }


def _worker_usage(db, worker_id: UUID) -> tuple[int, int, int, int, int]:
    rows = db.execute(
        text("""SELECT resources FROM resource_reservations
        WHERE worker_id=:worker AND status IN ('HELD','QUARANTINED')"""),
        {"worker": worker_id},
    ).mappings()
    cpu = mem = disk = model = browser = 0
    for row in rows:
        item = row["resources"]
        cpu += item["cpu_millicores"]
        mem += item["memory_bytes"]
        disk += item["disk_bytes"]
        model += item["model_slots"]
        browser += item["browser_slots"]
    return cpu, mem, disk, model, browser


def claim_activity(
    engine: Engine,
    subject: str,
    key: str,
    body: ClaimRequest,
    *,
    lease_ttl: timedelta = DEFAULT_LEASE_TTL,
) -> ActivityLease:
    path = "/internal/v1/claims"
    request_scope = json.dumps([subject, "POST", path, key], separators=(",", ":"))
    request_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with engine.begin() as db:
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
            return ActivityLease.model_validate(old["result"])

        worker = (
            db.execute(
                text("SELECT * FROM workers WHERE subject=:subject AND status='ACTIVE' FOR UPDATE"),
                {"subject": subject},
            )
            .mappings()
            .first()
        )
        if worker is None:
            raise WorkerForbidden()
        allowed = set(worker["allowed_kinds"] or [])
        requested = set(body.kinds)
        if not requested.issubset(allowed):
            raise WorkerForbidden()
        worker_caps = set(worker["capabilities"] or [])
        if body.capabilities and not set(body.capabilities).issubset(worker_caps):
            raise WorkerForbidden()
        kinds = list(requested)

        # 先结算失租，再领取；旧 epoch 永不复活。
        expire_stale_leases(db)

        empty = ActivityLease(lease=None)
        chosen = None
        for _ in range(8):
            row = (
                db.execute(
                    text("""SELECT * FROM activities
                    WHERE status='READY' AND kind = ANY(:kinds)
                    ORDER BY created_at,id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1"""),
                    {"kinds": kinds},
                )
                .mappings()
                .first()
            )
            if row is None:
                break
            trust = db.execute(
                text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
                {"id": row["project_id"]},
            ).scalar_one()
            if trust != "OPEN":
                continue
            # Temporal Goal 仅经具名 admit_runtime_attempt；禁止经全局 READY 扫描领取。
            # 强制 TEMPORAL 且未开 RING_LEGACY_CLAIM_DRAIN 时：连 LEGACY Goal 也不经全局 claim（禁双调度）。
            goal_row = None
            if row["goal_id"] is not None:
                goal_row = (
                    db.execute(
                        text(
                            """SELECT status, orchestration_backend, release_manifest_id
                            FROM goals WHERE id=:id FOR SHARE"""
                        ),
                        {"id": row["goal_id"]},
                    )
                    .mappings()
                    .one()
                )
                if goal_row["orchestration_backend"] == "TEMPORAL":
                    continue
                if temporal_orchestration_enforced() and not legacy_claim_drain_allowed():
                    continue
            if row["kind"] == "PLAN":
                if goal_row is None or goal_row["status"] != "PLANNING":
                    continue
            elif row["kind"] == "EXECUTE":
                if goal_row is None or goal_row["status"] != "RUNNING":
                    continue
            elif row["kind"] == "AUDIT":
                if row["target_type"] == "GOAL_REVIEW":
                    # 周期诊断：规划/执行/验收中均可领；终态与 BLOCKED 不领
                    if goal_row is None or goal_row["status"] not in (
                        "PLANNING",
                        "RUNNING",
                        "VERIFYING",
                    ):
                        continue
                elif goal_row is None or goal_row["status"] not in ("RUNNING", "VERIFYING"):
                    continue
            elif row["kind"] == "FINALIZE":
                if goal_row is None or goal_row["status"] != "VERIFYING":
                    continue
            elif row["kind"] == "INTEGRATE":
                if goal_row is None or goal_row["status"] != "RUNNING":
                    continue
            elif row["kind"] == "RECONCILE":
                # UNKNOWN 对账：Goal 仍在途或阻塞时均可领取
                if goal_row is not None and goal_row["status"] in ("DONE", "CANCELLED", "FAILED"):
                    continue
            elif row["kind"] == "EXPORT_EVIDENCE":
                # 只读导出：终态且已有 ReleaseManifest 才可领（含 DONE/FAILED/CANCELLED）
                if goal_row is not None:
                    if goal_row["release_manifest_id"] is None:
                        continue
                    if goal_row["status"] not in ("DONE", "CANCELLED", "FAILED"):
                        continue
            elif row["kind"] in ("VALIDATE_SKILL", "PROBE_MODEL", "INDEX_MEMORY"):
                # 无 Goal；项目 OPEN 已在上方检查。
                pass
            if row["goal_id"] is not None and row["kind"] in (
                "PLAN",
                "EXECUTE",
                "INTEGRATE",
                "AUDIT",
                "FINALIZE",
            ):
                from .budget_clock import assert_goal_wall_budget_allows_engineering

                try:
                    assert_goal_wall_budget_allows_engineering(db, row["goal_id"])
                except LeaseRejected:
                    # 墙钟耗尽/UNKNOWN：跳过该活动，不冒领
                    continue
            scope_lock = json.dumps(
                ["budget", str(row["project_id"]), str(row["budget_scope_id"])],
                separators=(",", ":"),
            )
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": scope_lock},
            )
            if binding_digest_of(row["binding"]) != binding_digest_of(_live_binding(db, row)):
                raise BindingStale()
            need = row["resources"]
            cpu, mem, disk, model, browser = _worker_usage(db, worker["id"])
            if (
                cpu + need["cpu_millicores"] > worker["cpu_millicores"]
                or mem + need["memory_bytes"] > worker["memory_bytes"]
                or disk + need["disk_bytes"] > worker["disk_bytes"]
                or model + need["model_slots"] > worker["model_slots"]
                or browser + need["browser_slots"] > worker["browser_slots"]
            ):
                continue

            chosen = row
            break

        if chosen is None:
            db.execute(
                text(
                    "INSERT INTO project_requests(scope,body_digest,result) VALUES(:scope,:digest,CAST(:result AS jsonb))"
                ),
                {
                    "scope": request_scope,
                    "digest": request_digest,
                    "result": empty.model_dump_json(),
                },
            )
            return empty

        # 防御：若选中行仍属 TEMPORAL Goal，拒绝落租（正常路径应已 continue）
        # 强制 TEMPORAL 且未开排空开关时，任何 Goal 活动亦拒绝全局 claim。
        if chosen["goal_id"] is not None:
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": chosen["goal_id"]},
            ).scalar_one()
            if backend == "TEMPORAL":
                raise LeaseRejected("INVALID_STATE", "TEMPORAL Goal 禁止全局 claim")
            if temporal_orchestration_enforced() and not legacy_claim_drain_allowed():
                raise LeaseRejected(
                    "INVALID_STATE",
                    "强制 TEMPORAL 时禁止全局 claim 领取 Goal 活动（须 RING_LEGACY_CLAIM_DRAIN）",
                )

        epoch = db.execute(
            text(
                "SELECT COALESCE(MAX(fencing_epoch),0)+1 FROM activity_attempts WHERE activity_id=:id"
            ),
            {"id": chosen["id"]},
        ).scalar_one()
        now = datetime.now(UTC)
        expires = now + lease_ttl
        attempt_id = uuid4()
        attempt_row = (
            db.execute(
                text("""INSERT INTO activity_attempts(
                  id,activity_id,project_id,worker_id,binding_digest,fencing_epoch,
                  lease_expires_at,renewal_seq,status,skill_versions,started_at)
                VALUES(
                  :id,:activity,:project,:worker,:binding,:epoch,
                  :expires,0,'ACTIVE','[]'::jsonb,:started)
                RETURNING *"""),
                {
                    "id": attempt_id,
                    "activity": chosen["id"],
                    "project": chosen["project_id"],
                    "worker": worker["id"],
                    "binding": binding_digest_of(chosen["binding"]),
                    "epoch": epoch,
                    "expires": expires,
                    "started": now,
                },
            )
            .mappings()
            .one()
        )
        updated = (
            db.execute(
                text("""UPDATE activities SET status='RUNNING',
                  state_revision=state_revision+1,
                  current_attempt_id=:attempt,
                  updated_at=clock_timestamp()
                  WHERE id=:id AND status='READY'
                  RETURNING *"""),
                {"id": chosen["id"], "attempt": attempt_id},
            )
            .mappings()
            .first()
        )
        if updated is None:
            raise LeaseRejected("INVALID_STATE", "活动已被其他 worker 领取")
        # 验证类活动：claim 成功后开立 VerificationObligation（幂等）
        from .obligations import open_obligations_for_claim

        open_obligations_for_claim(db, updated, attempt_id)
        if chosen["kind"] == "EXECUTE" and chosen["task_id"] is not None:
            db.execute(
                text("""UPDATE tasks SET status='RUNNING',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id AND status='READY'"""),
                {"id": chosen["task_id"]},
            )
        db.execute(
            text("""INSERT INTO resource_reservations(
              id,project_id,activity_id,attempt_id,worker_id,resources,status)
            VALUES(:id,:project,:activity,:attempt,:worker,CAST(:resources AS jsonb),'HELD')"""),
            {
                "id": uuid4(),
                "project": chosen["project_id"],
                "activity": chosen["id"],
                "attempt": attempt_id,
                "worker": worker["id"],
                "resources": json.dumps(chosen["resources"]),
            },
        )
        db.execute(
            text("""INSERT INTO budget_reservations(
              id,project_id,budget_scope_id,activity_id,attempt_id,status)
            VALUES(:id,:project,:scope,:activity,:attempt,'HELD')"""),
            {
                "id": uuid4(),
                "project": chosen["project_id"],
                "scope": chosen["budget_scope_id"],
                "activity": chosen["id"],
                "attempt": attempt_id,
            },
        )
        if chosen["goal_id"] is not None:
            policy_snapshot_id = UUID(
                db.execute(
                    text("SELECT contract->>'policy_id' FROM goals WHERE id=:id"),
                    {"id": chosen["goal_id"]},
                ).scalar_one()
            )
        else:
            policy_snapshot_id = UUID(
                db.execute(
                    text("""SELECT id::text FROM policies
                    WHERE project_id=:project ORDER BY version DESC LIMIT 1"""),
                    {"project": chosen["project_id"]},
                ).scalar_one()
            )
        activity = _activity_from_row(updated)
        attempt = _attempt_from_row(attempt_row)
        result = ActivityLease(
            lease=LeaseIdentity(
                activity_id=activity.id,
                attempt_id=attempt.id,
                fencing_epoch=str(epoch),
            ),
            activity=activity,
            attempt=attempt,
            input_artifact_ids=[],
            policy_snapshot_id=policy_snapshot_id,
            workspace_root=None,
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
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'ACTIVITY_CLAIMED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": chosen["project_id"],
                "payload": result.model_dump_json(),
            },
        )
        return result


def renew_lease(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: HeartbeatRequest,
    *,
    lease_ttl: timedelta = DEFAULT_LEASE_TTL,
) -> HeartbeatResponse:
    if body.lease.activity_id != activity_id:
        raise LeaseRejected("INVALID_REQUEST", "租约与路径活动不一致")
    goal_id_for_tick: UUID | None = None
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
        activity_goal = (
            db.execute(
                text("SELECT goal_id FROM activities WHERE id=:id"),
                {"id": activity_id},
            )
            .mappings()
            .first()
        )
        if activity_goal is not None and activity_goal["goal_id"] is not None:
            goal_id_for_tick = activity_goal["goal_id"]
        now = datetime.now(UTC)
        expires = attempt["lease_expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            raise LeaseRejected("LEASE_EXPIRED", "租约已过期，不能复活")
        current = attempt["renewal_seq"]
        control, pending = _heartbeat_control_for_attempt(
            db, activity_id=activity_id, attempt_id=attempt["id"]
        )
        if body.renewal_seq <= current:
            response = HeartbeatResponse(
                lease_expires_at=expires,
                renewal_seq=current,
                control=control,
                pending_stop_ids=pending,
            )
        elif body.renewal_seq != current + 1:
            raise LeaseRejected("INVALID_REQUEST", "renewal_seq 必须连续递增")
        else:
            new_expires = now + lease_ttl
            row = (
                db.execute(
                    text("""UPDATE activity_attempts
                    SET renewal_seq=:seq, lease_expires_at=:expires, updated_at=clock_timestamp()
                    WHERE id=:id
                    RETURNING lease_expires_at,renewal_seq"""),
                    {
                        "id": attempt["id"],
                        "seq": body.renewal_seq,
                        "expires": new_expires,
                    },
                )
                .mappings()
                .one()
            )
            response = HeartbeatResponse(
                lease_expires_at=row["lease_expires_at"],
                renewal_seq=row["renewal_seq"],
                control=control,
                pending_stop_ids=pending,
            )
    # 续约已提交：独立事务推进墙钟（Issue #20 残差——长跑不能只靠 claim/pause）
    if goal_id_for_tick is not None:
        from .budget_clock import tick_goal_budget_after_heartbeat

        tick_goal_budget_after_heartbeat(engine, goal_id_for_tick)
    return response
