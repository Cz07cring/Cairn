"""TM01 部分举证：PG 提交后 Relay 崩溃 / Temporal 启动后 ACK 丢失。

真实 PG + Fake Temporal；禁 LEGACY fallback。非真 Temporal Server，全量 TM01–TM12 仍开。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import get_delivery_for_command
from orchestration import (
    FakeTemporalClient,
    OrchestrationBindingContent,
    TemporalUnavailable,
    ensure_workflow,
)
from sqlalchemy import text
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal


def _binding_content(row) -> OrchestrationBindingContent:
    return OrchestrationBindingContent(
        project_id=row["project_id"],
        goal_id=row["goal_id"],
        budget_scope_id=row["budget_scope_id"],
        backend=row["backend"],
        owner_epoch=row["owner_epoch"],
        namespace=row["namespace"],
        workflow_id=row["workflow_id"],
        active_run_id=row["active_run_id"],
        worker_build_id=row["worker_build_id"],
        contract_digest=row["contract_digest"],
    )


def _load_binding(engine, goal_id: str) -> OrchestrationBindingContent:
    with engine.connect() as db:
        row = db.execute(
            text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
            {"goal": goal_id},
        ).mappings().one()
    return _binding_content(row)


def _assert_claim_empty(client, token) -> None:
    """全局 claim 对该 TEMPORAL Goal 必须空（无双派发）。"""
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


def _assert_single_temporal_binding(engine, goal_id: str) -> None:
    with engine.connect() as db:
        n = db.execute(
            text("SELECT count(*) FROM orchestration_bindings WHERE goal_id=:goal"),
            {"goal": goal_id},
        ).scalar_one()
        backend = db.execute(
            text("SELECT orchestration_backend FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
    assert n == 1
    assert backend == "TEMPORAL"


def test_tm01_relay_crash_before_ack_then_idempotent_relay(api, objects):
    """PG 提交后 Relay 崩溃：delivery 仍 PENDING；重试一次 ACK，再试同 run_id。"""
    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        with engine.connect() as db:
            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery is not None
            assert delivery["delivery_status"] == "PENDING"
            assert delivery["run_id"] is None

        _assert_claim_empty(client, token)
        _assert_single_temporal_binding(engine, goal["id"])

        # 模拟崩溃：尚未 relay；状态仍 PENDING
        with engine.connect() as db:
            still = get_delivery_for_command(db, UUID(command["id"]))
            assert still["delivery_status"] == "PENDING"

        binding = _load_binding(engine, goal["id"])
        fake = FakeTemporalClient()
        receipt = ensure_workflow(engine, binding, UUID(command["id"]), fake)
        assert receipt.delivery_status == "ACKNOWLEDGED"
        assert receipt.run_id is not None

        again = ensure_workflow(engine, binding, UUID(command["id"]), fake)
        assert again.run_id == receipt.run_id
        assert again.model_dump() == receipt.model_dump()

        with engine.connect() as db:
            acked = get_delivery_for_command(db, UUID(command["id"]))
            assert acked["delivery_status"] == "ACKNOWLEDGED"
            assert acked["run_id"] == receipt.run_id

        _assert_claim_empty(client, token)
        _assert_single_temporal_binding(engine, goal["id"])
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_tm01_ack_lost_reuses_existing_run_without_second_start(api, objects):
    """Temporal 已启动但 ACK 丢失：Fake 已有 workflow→run；ensure 只 ACK 不二次 start。"""
    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        binding = _load_binding(engine, goal["id"])
        known_run = f"run-preseed-{uuid4()}"
        fake = FakeTemporalClient()
        fake._runs[binding.workflow_id] = known_run

        with engine.connect() as db:
            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery["delivery_status"] == "PENDING"

        _assert_claim_empty(client, token)

        receipt = ensure_workflow(engine, binding, UUID(command["id"]), fake)
        assert receipt.delivery_status == "ACKNOWLEDGED"
        assert receipt.run_id == known_run
        assert fake._runs[binding.workflow_id] == known_run

        again = ensure_workflow(engine, binding, UUID(command["id"]), fake)
        assert again.run_id == known_run

        _assert_claim_empty(client, token)
        _assert_single_temporal_binding(engine, goal["id"])
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_tm01_unavailable_keeps_pending_no_legacy_flip(api, objects):
    """TemporalUnavailable：ensure 抛错；delivery 仍 PENDING；Goal 不翻 LEGACY。"""
    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        binding = _load_binding(engine, goal["id"])
        fake = FakeTemporalClient()
        fake.unavailable = True

        _assert_claim_empty(client, token)

        try:
            ensure_workflow(engine, binding, UUID(command["id"]), fake)
            raise AssertionError("expected TemporalUnavailable")
        except TemporalUnavailable:
            pass

        with engine.connect() as db:
            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery["delivery_status"] == "PENDING"
            assert delivery["run_id"] is None
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
            assert backend == "TEMPORAL"
            cmd_status = db.execute(
                text("SELECT status FROM command_operations WHERE id=:id"),
                {"id": command["id"]},
            ).scalar_one()
            assert cmd_status == "ACCEPTED"

        _assert_claim_empty(client, token)
        _assert_single_temporal_binding(engine, goal["id"])
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_tm01_global_claim_empty_throughout_pending_window(api, objects):
    """PENDING 窗口全程：同 Goal 仅一 TEMPORAL 绑定，全局 claim 始终空。"""
    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        _assert_claim_empty(client, token)
        _assert_single_temporal_binding(engine, goal["id"])

        with engine.connect() as db:
            assert get_delivery_for_command(db, UUID(command["id"]))["delivery_status"] == "PENDING"

        # 仍不 relay：再断言一次 claim
        _assert_claim_empty(client, token)

        # 确认 goals 表未因任何路径写回 LEGACY
        with engine.connect() as db:
            backend = db.execute(
                text("SELECT orchestration_backend FROM goals WHERE id=:id"),
                {"id": goal["id"]},
            ).scalar_one()
            assert backend == "TEMPORAL"
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()
