"""AB04 命名缝：checkpoint/意图过期 → 域裁决 → Kernel 持久化 BLOCKED；≠ DONE。

把第一百二十七批域裁决与 Issue #24 放弃持久化串成单一可挂台账的集成缝。
编排 Workflow 门3 接线仍属 Hermes；本缝只证明 Kernel 侧可观测 abandoned/block。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from control_kernel.domain.recovery_intent import (
    RECOVERY_CHECKPOINT_SCHEMA_VERSION,
    RecoveryAbandon,
    evaluate_generation_recovery,
)
from control_kernel.storage.abandonments import (
    list_orchestration_abandonments,
    record_orchestration_abandonment,
)
from test_claims import _register_worker, _start_goal


def test_ab04_intent_expired_persists_block_not_done(api, objects):
    """意图截止已过 → INTENT_EXPIRED → Goal BLOCKED + abandonment 行；≠ DONE。"""
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])

    past = datetime(2020, 1, 1, tzinfo=UTC)
    decision = evaluate_generation_recovery(
        generation=1,
        recovery_enabled=True,
        checkpoint_schema_version=RECOVERY_CHECKPOINT_SCHEMA_VERSION,
        intent_valid_until=past,
        recovery_attempts=0,
        max_recovery_attempts=3,
        now=datetime.now(UTC),
    )
    assert isinstance(decision, RecoveryAbandon)
    assert decision.reason == "INTENT_EXPIRED"
    assert decision.marks_goal_done is False

    worker = str(uuid4())
    _register_worker(worker, kinds=("PLAN", "EXECUTE"))
    row = record_orchestration_abandonment(
        engine,
        goal_id,
        subject=worker,
        project_ids=[],
        reason=decision.reason,
        generation=1,
        prior_run_id="ab04-intent-expired",
    )
    assert row["marks_goal_done"] is False
    assert row["reason"] == "INTENT_EXPIRED"

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "BLOCKED"
    assert got["status"] != "DONE"
    assert got["block_reason"] == "ORCHESTRATION_ABANDONED:INTENT_EXPIRED"

    listed = list_orchestration_abandonments(engine, goal_id, subject=worker, project_ids=[])
    assert len(listed) == 1
    assert listed[0]["reason"] == "INTENT_EXPIRED"
    assert listed[0]["marks_goal_done"] is False


def test_ab04_schema_incompatible_persists_block_not_done(api, objects):
    """checkpoint schema 不兼容 → CHECKPOINT_SCHEMA_INCOMPATIBLE → BLOCKED；≠ DONE。"""
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])

    future = datetime.now(UTC) + timedelta(hours=2)
    decision = evaluate_generation_recovery(
        generation=2,
        recovery_enabled=True,
        checkpoint_schema_version=99,
        intent_valid_until=future,
        recovery_attempts=0,
        max_recovery_attempts=3,
        now=datetime.now(UTC),
    )
    assert isinstance(decision, RecoveryAbandon)
    assert decision.reason == "CHECKPOINT_SCHEMA_INCOMPATIBLE"
    assert decision.marks_goal_done is False

    worker = str(uuid4())
    _register_worker(worker, kinds=("PLAN", "EXECUTE"))
    row = record_orchestration_abandonment(
        engine,
        goal_id,
        subject=worker,
        project_ids=[],
        reason=decision.reason,
        generation=2,
        prior_run_id="ab04-schema-incompatible",
    )
    assert row["marks_goal_done"] is False

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "BLOCKED"
    assert got["status"] != "DONE"
    assert (
        got["block_reason"]
        == "ORCHESTRATION_ABANDONED:CHECKPOINT_SCHEMA_INCOMPATIBLE"
    )
