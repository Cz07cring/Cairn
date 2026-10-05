"""INDEX_MEMORY：排队无 Goal 索引活动；outcome 将诚实 PROPOSED 记忆晋升为 VERIFIED。

本切片不建 memory_indexes 表：只更新 memories.status，并把 outcome.index_version
字符串写入命令结果与事件（不捏造索引字节）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from ..protocols.memory import IndexMemorySuccessOutcome
from ..protocols.plans import ActivityOutcomeRequest
from ..protocols.runtime import (
    CommandOperation,
    CommandResult,
    ExecutionBinding,
    LeaseRejected,
    PlanRejected,
    Resources,
    StateRevisionConflict,
    WorkerForbidden,
)
from .activities import _activity_from_row, _command_from_row
from .policies import ConfigurationVersions, ScopeNotFound, TrustBlocked
from .projects import ProjectConflict

INDEX_RESOURCES = Resources(
    cpu_millicores=100,
    memory_bytes=268435456,
    disk_bytes=67108864,
    model_slots=0,
    browser_slots=0,
    exclusive_labels=[],
)


def memory_ids_digest(memory_ids: list[UUID]) -> str:
    """对排序后的记忆 id 做稳定摘要，供 binding.subject_digest 使用。"""
    payload = json.dumps(
        [str(i) for i in sorted(memory_ids, key=str)],
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def start_index_memory(
    engine: Engine,
    project_id: UUID,
    subject: str,
    project_ids: list[str],
    key: str,
) -> CommandOperation:
    """有 PROPOSED 记忆时排队 READY INDEX_MEMORY；无 Goal/Task，budget=project。"""
    path = f"/internal/v1/projects/{project_id}/memory-index"
    request_scope = json.dumps(
        [str(project_id), subject, "POST", path, key], separators=(",", ":")
    )
    request_digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    with engine.begin() as db:
        ConfigurationVersions.check_scope(db, project_id, subject, project_ids)
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:id FOR SHARE"),
            {"id": project_id},
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

        open_act = db.execute(
            text(
                """SELECT 1 FROM activities
                WHERE kind='INDEX_MEMORY' AND target_type='MEMORY_INDEX' AND target_id=:project
                  AND status IN ('READY','RUNNING','WAITING','RECOVERING')
                LIMIT 1"""
            ),
            {"project": project_id},
        ).first()
        if open_act is not None:
            raise PlanRejected("已有进行中的记忆索引活动")

        proposed = (
            db.execute(
                text(
                    """SELECT id FROM memories
                    WHERE project_id=:project AND status='PROPOSED'
                    ORDER BY id"""
                ),
                {"project": project_id},
            )
            .scalars()
            .all()
        )
        if not proposed:
            raise PlanRejected("项目没有待索引的 PROPOSED 记忆")

        policy = (
            db.execute(
                text(
                    """SELECT content_digest FROM policies
                    WHERE project_id=:project ORDER BY version DESC, created_at DESC LIMIT 1"""
                ),
                {"project": project_id},
            )
            .mappings()
            .first()
        )
        if policy is None:
            raise PlanRejected("项目尚无策略")

        subject_digest = memory_ids_digest(list(proposed))
        binding = ExecutionBinding(
            goal_contract_revision=None,
            goal_contract_digest=None,
            task_contract_revision=None,
            task_contract_digest=None,
            plan_revision=None,
            subject_digest=subject_digest,
            policy_digest=policy["content_digest"],
            model_profile_digest=None,
            skill_set_digest=None,
        )
        activity_id = uuid4()
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,NULL,NULL,:project,'INDEX_MEMORY','MEMORY_INDEX',:project,
                  CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": activity_id,
                "project": project_id,
                "binding": binding.model_dump_json(),
                "resources": INDEX_RESOURCES.model_dump_json(),
            },
        )
        command_id = uuid4()
        result = CommandResult(activity_id=activity_id)
        command_row = (
            db.execute(
                text(
                    """INSERT INTO command_operations(
                      id,project_id,goal_id,kind,status,request_digest,result,error,subject)
                    VALUES(
                      :id,:project,NULL,'INDEX_MEMORY','RUNNING',:digest,
                      CAST(:result AS jsonb),NULL,:subject)
                    RETURNING *"""
                ),
                {
                    "id": command_id,
                    "project": project_id,
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
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'MEMORY_INDEX_STARTED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": project_id,
                "payload": command.model_dump_json(),
            },
        )
        return command


def submit_index_outcome(
    engine: Engine,
    subject: str,
    activity_id: UUID,
    body: ActivityOutcomeRequest,
):
    """将 outcome.record_ids 中仍为 PROPOSED 的记忆晋升 VERIFIED（证据路径诚实核对）。"""
    if not isinstance(body.outcome, IndexMemorySuccessOutcome):
        raise PlanRejected("INDEX_MEMORY 需要 IndexMemorySuccessOutcome")
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
        if activity["kind"] != "INDEX_MEMORY":
            raise PlanRejected("非 INDEX_MEMORY 活动")
        if activity["status"] != "RUNNING":
            raise LeaseRejected("INVALID_STATE", "活动不在 RUNNING")
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

        # outcome 侧证据仅作索引过程证明；存在则须属于本项目
        for evidence_id in outcome.evidence_ids:
            exists = db.execute(
                text(
                    """SELECT 1 FROM artifacts
                    WHERE project_id=:project AND id=:id"""
                ),
                {"project": activity["project_id"], "id": evidence_id},
            ).first()
            if exists is None:
                raise PlanRejected("outcome.evidence_ids 含未知工件")

        verified_ids: list[str] = []
        for record_id in outcome.record_ids:
            row = (
                db.execute(
                    text(
                        """SELECT * FROM memories
                        WHERE id=:id AND project_id=:project FOR UPDATE"""
                    ),
                    {"id": record_id, "project": activity["project_id"]},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise PlanRejected("record_ids 含未知或不属于本项目的记忆")
            if row["status"] != "PROPOSED":
                raise PlanRejected(f"记忆 {record_id} 状态为 {row['status']}，不可晋升 VERIFIED")
            # confidence_bp 不足以晋升；须核对 source_evidence_ids（空列表视为无源证据可索引）
            source_ids = list(row["source_evidence_ids"] or [])
            for source_id in source_ids:
                exists = db.execute(
                    text(
                        """SELECT 1 FROM artifacts
                        WHERE project_id=:project AND id=:id"""
                    ),
                    {"project": activity["project_id"], "id": source_id},
                ).first()
                if exists is None:
                    raise PlanRejected(
                        "source_evidence_ids 指向的工件已不存在，不能晋升 VERIFIED"
                    )
            db.execute(
                text(
                    """UPDATE memories SET status='VERIFIED', updated_at=clock_timestamp()
                    WHERE id=:id AND project_id=:project AND status='PROPOSED'"""
                ),
                {"id": record_id, "project": activity["project_id"]},
            )
            verified_ids.append(str(record_id))

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
            activity_id=activity_id,
            index_version=outcome.index_version,
            record_ids=list(outcome.record_ids),
            evidence_ids=list(outcome.evidence_ids),
        )
        db.execute(
            text(
                """UPDATE command_operations
                  SET status='SUCCEEDED', result=CAST(:result AS jsonb),
                      updated_at=clock_timestamp()
                  WHERE kind='INDEX_MEMORY' AND status='RUNNING'
                    AND result->>'activity_id'=:activity"""
            ),
            {
                "result": cmd_result.model_dump_json(),
                "activity": str(activity_id),
            },
        )
        db.execute(
            text(
                "INSERT INTO project_events(id,project_id,kind,payload) VALUES(:id,:project,'MEMORY_INDEXED',CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid4(),
                "project": activity["project_id"],
                "payload": json.dumps(
                    {
                        "activity_id": str(activity_id),
                        "index_version": outcome.index_version,
                        "record_ids": verified_ids,
                        "evidence_ids": [str(i) for i in outcome.evidence_ids],
                    },
                    separators=(",", ":"),
                ),
            },
        )
        return _activity_from_row(updated)
