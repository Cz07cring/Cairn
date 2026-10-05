"""TM11 部分举证：LEGACY↔TEMPORAL 切换守卫；每 Goal 唯一 owner、无双派发。

真实 PG；禁 LEGACY fallback 掩盖 Temporal 失败；DRAFT/idle 安全切换后 claim 空。
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from control_kernel.storage.goals import (
    UnsafeOrchestrationBackendSwitch,
    set_goal_orchestration_backend,
)
from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _register_worker, _start_goal
from test_goals import _ready_project
from test_orchestration_backend import _start_temporal_goal


def _assert_claim_empty(client, token) -> None:
    subject = str(uuid4())
    _register_worker(subject)
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(subject, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["lease"] is None


def test_tm11_refuse_switch_with_active_attempt(api, objects):
    """有 ACTIVE attempt 时禁止翻 TEMPORAL（防双派发）。"""
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        subject = str(uuid4())
        _register_worker(subject)
        claimed = client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={
                "Authorization": "Bearer " + token(subject, ["worker"]),
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["data"]["lease"] is not None

        with pytest.raises(UnsafeOrchestrationBackendSwitch, match="ACTIVE attempt"):
            set_goal_orchestration_backend(
                engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1"
            )

        with engine.connect() as db:
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
        assert backend == "LEGACY"
    finally:
        _drain_ready_plans(client, token)
        engine.dispose()


def test_tm11_refuse_switch_with_unknown_effect(api, objects):
    """DISPATCHED/UNKNOWN effect 未对账时禁止切换。"""
    client, token, _auth, goal, plan = _start_goal(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        subject = str(uuid4())
        worker_id = _register_worker(subject)
        # 造最小 UNKNOWN effect：无 ACTIVE attempt，仅挂在 Goal 上
        with engine.begin() as db:
            attempt_id = uuid4()
            artifact_id = uuid4()
            effect_id = uuid4()
            step_id = uuid4()
            digest = "sha256:" + ("ab" * 32)
            binding = db.execute(
                text("SELECT binding FROM activities WHERE id=:id"),
                {"id": plan["id"]},
            ).scalar_one()
            from control_kernel.storage.claims import binding_digest_of

            db.execute(
                text(
                    """INSERT INTO artifacts(
                      id,project_id,digest,size_bytes,mime,representation,producer_identity)
                    VALUES (:id,:project,:digest,0,'application/octet-stream','RAW','tm11')"""
                ),
                {"id": artifact_id, "project": goal["project_id"], "digest": digest},
            )
            db.execute(
                text(
                    """INSERT INTO activity_attempts(
                      id,activity_id,project_id,worker_id,binding_digest,fencing_epoch,
                      lease_expires_at,renewal_seq,status,skill_versions,started_at,finished_at)
                    VALUES (
                      :id,:activity,:project,:worker,:binding,1,
                      clock_timestamp() + interval '1 hour',0,'COMPLETED','[]'::jsonb,
                      clock_timestamp(),clock_timestamp())"""
                ),
                {
                    "id": attempt_id,
                    "activity": plan["id"],
                    "project": goal["project_id"],
                    "worker": UUID(worker_id),
                    "binding": binding_digest_of(binding),
                },
            )
            db.execute(
                text(
                    """INSERT INTO effect_intents(
                      id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                      payload_digest,tool_ref,replay_class,scope,status,
                      input_artifact_id,producer_attempt_id)
                    VALUES (
                      :id,:project,:goal,:activity,:step,1,
                      :digest,'read_file','READ_ONLY','ENGINEERING','UNKNOWN',
                      :artifact,:attempt)"""
                ),
                {
                    "id": effect_id,
                    "project": goal["project_id"],
                    "goal": goal["id"],
                    "activity": plan["id"],
                    "step": step_id,
                    "digest": digest,
                    "artifact": artifact_id,
                    "attempt": attempt_id,
                },
            )

        with pytest.raises(UnsafeOrchestrationBackendSwitch, match="DISPATCHED/UNKNOWN"):
            set_goal_orchestration_backend(
                engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1"
            )
    finally:
        with engine.begin() as db:
            db.execute(
                text("DELETE FROM effect_intents WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            )
        _drain_ready_plans(client, token)
        engine.dispose()


def test_tm11_refuse_temporal_to_legacy_while_ready_without_force(api, objects):
    """TEMPORAL 上仍有 READY/RUNNING 时无 force 禁止回退 LEGACY。"""
    client, token, _auth, goal, plan, _command, engine = _start_temporal_goal(api, objects)
    try:
        with engine.connect() as db:
            status = db.execute(
                text("SELECT status FROM activities WHERE id=:id"),
                {"id": plan["id"]},
            ).scalar_one()
        assert status == "READY"

        with pytest.raises(UnsafeOrchestrationBackendSwitch, match="READY/RUNNING"):
            set_goal_orchestration_backend(
                engine, UUID(goal["id"]), "LEGACY", owner_epoch="1"
            )

        with engine.connect() as db:
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
        assert backend == "TEMPORAL"

        # 显式 force 仅绕过 READY/RUNNING（测试排空）
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        with engine.connect() as db:
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
        assert backend == "LEGACY"
    finally:
        _drain_ready_plans(client, token)
        engine.dispose()


def test_tm11_safe_draft_switch_unique_owner_no_dual_dispatch(api, objects):
    """DRAFT 安全翻 TEMPORAL → START：全局 claim 空；仅一 orchestration_bindings。"""
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    _drain_ready_plans(client, token)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1"
        )
        started = client.post(
            f"/api/v1/goals/{goal['id']}/start",
            json={"expected_state_revision": 1, "reason": "tm11"},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert started.status_code == 202, started.text

        _assert_claim_empty(client, token)
        with engine.connect() as db:
            n = db.execute(
                text("SELECT count(*) FROM orchestration_bindings WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).scalar_one()
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
        assert n == 1
        assert backend == "TEMPORAL"
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
