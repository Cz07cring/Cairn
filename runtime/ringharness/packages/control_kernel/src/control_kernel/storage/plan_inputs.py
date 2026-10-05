"""Cairn PlanInput/v1 持久化（R1a）：事务内核验后封存字节，append-only。

红线：
- 候选不信 Cairn 复制值：同事务重读 Ring plans 行，重算 candidate digest 与
  受限摘要，逐字段比对 payload；不符失败关闭。
- Goal 合同/计划版本以 DB 当前值为准；payload 声明的 contract_digest 必须等于
  goals.contract_digest。
- 保留 canonical 原始字节（bytea），读回可逐字节复现 digest。
- 幂等 scope 覆盖 (goal, subject, POST, path, Idempotency-Key)，body digest 覆盖
  完整固定正文（含 candidate_plan_id）；同键异体失败关闭。
- 只写 plan_inputs / project_requests / project_events / goal_events；不改 Goal、
  不建 Plan/Task、不派发、绝不写 DONE。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.goals import GoalResource, PlanInputModeSelection
from ..protocols.plan_inputs import (
    MAX_COVERAGE,
    MAX_TASKS,
    PlanInputPayload,
    PlanInputResource,
    canonical_json,
)
from ..protocols.runtime import InvalidGoalState, PlanRejected, StateRevisionConflict
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict


def select_plan_input_mode(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    body: PlanInputModeSelection,
) -> GoalResource:
    """仅 DRAFT 可选；Goal 行锁与 state_revision 防止 start 并发越过选择。"""
    from .goals import _row_to_resource

    with engine.begin() as db:
        goal = (
            db.execute(text("SELECT * FROM goals WHERE id=:id FOR UPDATE"), {"id": goal_id})
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
        if trust != "OPEN":
            raise TrustBlocked()
        if goal["status"] != "DRAFT":
            raise InvalidGoalState(goal["status"])
        if body.mode == "REQUIRED" and goal["orchestration_backend"] != "TEMPORAL":
            raise PlanRejected("PLAN_INPUT_MODE_BACKEND: REQUIRED 仅支持 TEMPORAL Goal")
        if goal["state_revision"] != body.expected_state_revision:
            raise StateRevisionConflict()
        updated = (
            db.execute(
                text("""UPDATE goals SET plan_input_mode=:mode,
                state_revision=state_revision+1, updated_at=clock_timestamp()
                WHERE id=:id RETURNING *"""),
                {"id": goal_id, "mode": body.mode},
            )
            .mappings()
            .one()
        )
        result = _row_to_resource(updated)
        db.execute(
            text("""INSERT INTO project_events(id,project_id,kind,payload)
              VALUES(:id,:project,'GOAL_STATE_CHANGED',CAST(:payload AS jsonb))"""),
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


def resolve_required_plan_input(db, goal) -> PlanInputResource:
    """从当前 Goal 版本选择唯一有效 STAGED 输入；无/多/漂移均拒绝。"""
    newest = db.execute(
        text("""SELECT id FROM plans WHERE goal_id=:goal AND plan_revision IS NULL
          ORDER BY created_at DESC,id DESC LIMIT 1"""),
        {"goal": goal["id"]},
    ).scalar_one_or_none()
    if newest is None:
        raise PlanRejected("PLAN_INPUT_REQUIRED: 当前 Goal 没有 CANDIDATE")
    rows = (
        db.execute(
            text("""SELECT * FROM plan_inputs WHERE goal_id=:goal
          AND goal_contract_revision=:revision AND expected_plan_revision IS NOT DISTINCT FROM :plan_revision
          AND candidate_plan_id=:candidate AND status='STAGED' ORDER BY created_at,id"""),
            {
                "goal": goal["id"],
                "revision": goal["contract_revision"],
                "plan_revision": goal["plan_revision"],
                "candidate": newest,
            },
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise PlanRejected("PLAN_INPUT_REQUIRED: 当前合同与计划修订必须恰有一份 STAGED 输入")
    resource = _row_to_resource(rows[0])
    payload = PlanInputPayload.model_validate(resource.payload)
    if (
        resource.project_id != goal["project_id"]
        or resource.goal_id != goal["id"]
        or payload.goal_contract_digest != goal["contract_digest"]
        or payload.candidate_plan_id != resource.candidate_plan_id
        or payload.candidate_content_digest != resource.candidate_content_digest
        or payload.goal_contract_revision != goal["contract_revision"]
        or payload.expected_plan_revision != goal["plan_revision"]
    ):
        raise PlanRejected("PLAN_INPUT_STALE: 输入与 Goal 或候选钉扎不一致")
    _reverify_candidate(db, goal=goal, candidate_id=resource.candidate_plan_id, payload=payload)
    return resource


def assert_attempt_plan_input(db, activity, attempt, *, content: dict | None = None):
    """重核 attempt 固定输入，防编译、绑定或 outcome 改用其他候选。"""
    goal = (
        db.execute(text("SELECT * FROM goals WHERE id=:id"), {"id": activity["goal_id"]})
        .mappings()
        .one()
    )
    if goal["plan_input_mode"] != "REQUIRED" or activity["kind"] != "PLAN":
        return None
    selected = resolve_required_plan_input(db, goal)
    if (
        attempt["plan_input_id"] != selected.id
        or attempt["plan_input_digest"] != selected.content_digest
    ):
        raise PlanRejected("PLAN_INPUT_BINDING_STALE: attempt 固定输入与当前输入不符")
    if content is not None:
        refs = [
            r for r in content.get("input_bindings", []) if r.get("classification") == "CANDIDATE"
        ]
        if len(refs) != 1 or refs[0].get("digest") != selected.candidate_content_digest:
            raise PlanRejected("PLAN_INPUT_CONTEXT_MISMATCH: ContextBundle 缺少固定候选")
        artifact = db.execute(
            text("""SELECT id FROM artifacts WHERE id=:id AND project_id=:project
              AND digest=:digest"""),
            {
                "id": refs[0].get("artifact_id"),
                "project": goal["project_id"],
                "digest": selected.candidate_content_digest,
            },
        ).scalar_one_or_none()
        if artifact is None:
            raise PlanRejected("PLAN_INPUT_CONTEXT_MISMATCH: 候选工件目录与固定摘要不一致")
    return selected


# 与 Cairn seal_snapshot 一致的可接受 Goal 状态（首场景；DRAFT 预登记归 R1b）。
_ACCEPTING_GOAL_STATUSES = frozenset({"PLANNING", "RUNNING"})


def _digest(canonical: bytes) -> str:
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def payload_canonical_bytes(payload: PlanInputPayload) -> bytes:
    # by_alias：线上字节必须用 "schema"（与 Cairn seal_snapshot 完全一致）。
    return canonical_json(payload.model_dump(mode="json", by_alias=True)).encode("utf-8")


def _derive_candidate_summary(tasks: list[dict], coverage: list[dict]) -> dict[str, Any]:
    """从 Ring plans 行重算 Cairn 受限摘要（与 _candidate_summary 同口径）。

    tasks/coverage 为已入库的规范 JSON（PlanTaskNode/CoverageEntry 序列化结果）。
    """
    if not 1 <= len(tasks) <= MAX_TASKS:
        raise PlanRejected("PLAN_INPUT_CANDIDATE_SUMMARY: 候选任务数越界")
    if not 1 <= len(coverage) <= MAX_COVERAGE:
        raise PlanRejected("PLAN_INPUT_CANDIDATE_SUMMARY: 候选覆盖数越界")
    task_items = []
    for task in tasks:
        contract = task.get("contract") or {}
        task_items.append(
            {
                "id": str(task["id"]),
                "objective": contract.get("objective", ""),
                "depends_on": sorted(str(d) for d in contract.get("depends_on", [])),
            }
        )
    if len({item["id"] for item in task_items}) != len(task_items):
        raise PlanRejected("PLAN_INPUT_CANDIDATE_SUMMARY: 候选任务 ID 不唯一")
    coverage_items = [
        {
            "goal_criterion_id": entry["goal_criterion_id"],
            "task_id": str(entry["task_id"]),
            "task_acceptance_id": entry["task_acceptance_id"],
            "verification_profile_id": str(entry["verification_profile_id"]),
        }
        for entry in coverage
    ]
    known = {item["id"] for item in task_items}
    if any(item["task_id"] not in known for item in coverage_items):
        raise PlanRejected("PLAN_INPUT_CANDIDATE_SUMMARY: 覆盖引用未知任务")
    return {
        "task_count": len(task_items),
        "tasks": sorted(task_items, key=lambda item: item["id"]),
        "coverage": sorted(coverage_items, key=lambda item: tuple(item.values())),
    }


def _reverify_candidate(db, *, goal, candidate_id: UUID, payload: PlanInputPayload) -> None:
    """同事务重取 CANDIDATE 并重算比对；只读，绝不改写候选状态。"""
    row = (
        db.execute(
            text("SELECT * FROM plans WHERE id=:id"),
            {"id": candidate_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["project_id"] != goal["project_id"] or row["goal_id"] != goal["id"]:
        raise PlanRejected("PLAN_INPUT_CANDIDATE_NOT_FOUND: 候选不存在或不属于该 Goal")
    if row["status"] != "CANDIDATE" or row["plan_revision"] is not None:
        raise PlanRejected(
            f"PLAN_INPUT_CANDIDATE_STALE: 计划状态为 {row['status']}，不是当前 CANDIDATE"
        )
    if row["content_digest"] is None or row["content_digest"] != payload.candidate_content_digest:
        raise PlanRejected("PLAN_INPUT_CANDIDATE_DIGEST_MISMATCH: 候选摘要与 Ring DB 不一致")
    candidate_body = {
        "expected_plan_revision": payload.expected_plan_revision,
        "reason": row["reason"],
        "tasks": row["tasks"],
        "coverage": row["coverage"],
    }
    from ..protocols.plans import PlanCreate

    parsed = PlanCreate.model_validate(candidate_body)
    canonical = json.dumps(
        {
            "goal_id": str(goal["id"]),
            "goal_contract_revision": goal["contract_revision"],
            "plan": parsed.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if _digest(canonical) != row["content_digest"]:
        raise PlanRejected("PLAN_INPUT_CANDIDATE_DRIFT: 候选正文无法复现摘要")
    derived = _derive_candidate_summary(list(row["tasks"]), list(row["coverage"]))
    if derived != payload.candidate_summary.model_dump(mode="json"):
        raise PlanRejected("PLAN_INPUT_CANDIDATE_SUMMARY_MISMATCH: 候选摘要与 Ring DB 重算不一致")


def record_plan_input(
    engine: Engine,
    goal_id: UUID,
    *,
    subject: str,
    project_ids: list[str],
    key: str,
    body: PlanInputPayload,
) -> PlanInputResource:
    """幂等登记一条 PlanInput：事务内核验 Goal/候选/主体后封存 canonical 字节。

    同键同体回放缓存结果；同键异体 ProjectConflict（失败关闭，不静默换绑定）。
    """
    path = f"/api/v1/goals/{goal_id}/plan-inputs"
    request_scope = json.dumps([str(goal_id), subject, "POST", path, key], separators=(",", ":"))
    raw = payload_canonical_bytes(body)
    request_digest = _digest(raw)
    content_digest = request_digest

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
            cached = PlanInputResource.model_validate(old["result"])
            saved = (
                db.execute(
                    text("SELECT * FROM plan_inputs WHERE id=:id AND goal_id=:goal"),
                    {"id": cached.id, "goal": goal_id},
                )
                .mappings()
                .first()
            )
            if saved is None:
                raise RuntimeError("plan_input 幂等记录缺少权威输入行")
            result = _row_to_resource(saved)
            if result != cached or result.content_digest != request_digest:
                raise RuntimeError("plan_input 幂等缓存与权威输入行不一致")
            return result
        if trust != "OPEN":
            raise TrustBlocked()
        # payload 声明必须与路径/主体/DB 当前事实一致（全部失败关闭）。
        if body.ring_goal_id != goal["id"]:
            raise PlanRejected("PLAN_INPUT_GOAL_MISMATCH: ring_goal_id 与路径 Goal 不一致")
        if body.ring_project_id != goal["project_id"]:
            raise PlanRejected("PLAN_INPUT_PROJECT_MISMATCH: ring_project_id 与 Goal 项目不一致")
        if body.created_by != subject:
            raise PlanRejected("PLAN_INPUT_ACTOR_MISMATCH: created_by 与认证主体不一致")
        if body.goal_contract_revision != goal["contract_revision"]:
            raise PlanRejected("PLAN_INPUT_CONTRACT_REVISION_STALE: 合同修订与 Goal 当前值不一致")
        if body.goal_contract_digest != goal["contract_digest"]:
            raise PlanRejected("PLAN_INPUT_CONTRACT_DIGEST_MISMATCH: 合同摘要与 Goal 当前值不一致")
        if body.expected_plan_revision != goal["plan_revision"]:
            raise PlanRejected(
                "PLAN_INPUT_PLAN_REVISION_STALE: expected_plan_revision 与 Goal 当前值不一致"
            )
        if goal["status"] not in _ACCEPTING_GOAL_STATUSES:
            raise PlanRejected(f"PLAN_INPUT_GOAL_STATE: Goal 状态 {goal['status']} 不接受规划输入")
        _reverify_candidate(db, goal=goal, candidate_id=body.candidate_plan_id, payload=body)
        row = (
            db.execute(
                text(
                    """INSERT INTO plan_inputs(
                      id,project_id,goal_id,status,payload,payload_bytes,
                      content_digest,candidate_plan_id,
                      candidate_content_digest,goal_contract_revision,
                      expected_plan_revision,created_by,created_at)
                    VALUES(
                      :id,:project,:goal,'STAGED',CAST(:payload AS jsonb),:payload_bytes,
                      :content_digest,:candidate,:candidate_digest,
                      :contract_rev,:plan_rev,:created_by,CAST(:created_at AS timestamptz))
                    RETURNING *"""
                ),
                {
                    "id": uuid4(),
                    "project": goal["project_id"],
                    "goal": goal_id,
                    "payload": raw.decode("utf-8"),
                    # bytea 是权威字节账本；jsonb 仅为可查询投影（0039 复算口径）。
                    "payload_bytes": raw,
                    "content_digest": content_digest,
                    "candidate": body.candidate_plan_id,
                    "candidate_digest": body.candidate_content_digest,
                    "contract_rev": body.goal_contract_revision,
                    "plan_rev": body.expected_plan_revision,
                    "created_by": subject,
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            .mappings()
            .one()
        )
        result = _row_to_resource(row)
        db.execute(
            text(
                "INSERT INTO project_requests(scope,body_digest,result)"
                " VALUES(:scope,:digest,CAST(:result AS jsonb))"
            ),
            {
                "scope": request_scope,
                "digest": request_digest,
                "result": result.model_dump_json(),
            },
        )
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload)"
                " VALUES(:id,:project,'PLAN_INPUT_RECORDED',CAST(:payload AS jsonb))"
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
            event_type="PLAN_INPUT_RECORDED",
            entity_id=result.id,
            entity_state_revision=None,
            resource_type="PLAN_INPUT",
        )
        return result


def _row_to_resource(row) -> PlanInputResource:
    """行 → 资源；payload 一律从 bytea 权威账本解析，jsonb 仅核对一致性。

    双列一致性（coordinator 红线）：
    1. payload_bytes 重新 canonical 化后必须复现 content_digest（字节账本自洽）；
    2. jsonb 投影必须与字节解析结果深度相等（旁路写坏投影也失败关闭，不返回
       一个 digest 证明不了的 payload）。
    """
    data = dict(row)
    raw = bytes(data["payload_bytes"])
    payload = json.loads(raw.decode("utf-8"))
    if _digest(raw) != data["content_digest"]:
        raise RuntimeError("plan_input 存储字节无法复现 content_digest")
    if canonical_json(payload).encode("utf-8") != raw:
        raise RuntimeError("plan_input 存储字节不是规范 JSON")
    projection = data["payload"]
    if isinstance(projection, str):
        projection = json.loads(projection)
    if projection != payload:
        raise RuntimeError("plan_input jsonb 投影与权威字节不一致")
    return PlanInputResource.model_validate(
        {
            "id": data["id"],
            "project_id": data["project_id"],
            "goal_id": data["goal_id"],
            "status": data["status"],
            "created_by": data["created_by"],
            "created_at": data["created_at"],
            "content_digest": data["content_digest"],
            "candidate_plan_id": data["candidate_plan_id"],
            "candidate_content_digest": data["candidate_content_digest"],
            "goal_contract_revision": data["goal_contract_revision"],
            "expected_plan_revision": data["expected_plan_revision"],
            "payload": payload,
        }
    )


def get_plan_input(
    engine: Engine,
    *,
    goal_id: UUID,
    plan_input_id: UUID,
    subject: str,
    project_ids: list[str],
) -> PlanInputResource:
    """Goal scoped 读取：ID 与 goal_id 不符统一 404；读回复算 digest 失败关闭。"""
    with engine.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM plan_inputs WHERE id=:id AND goal_id=:goal"),
                {"id": plan_input_id, "goal": goal_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ScopeNotFound()
        ConfigurationVersions.check_scope(db, row["project_id"], subject, project_ids)
        # 字节账本自校验 + 双列一致性在 _row_to_resource 单点执行。
        return _row_to_resource(row)
