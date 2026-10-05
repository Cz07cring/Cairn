"""Temporal M0/M1：LEGACY claim；TEMPORAL START→delivery→relay→admit。"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import admit_runtime_attempt, get_delivery_for_command
from orchestration import (
    FakeTemporalClient,
    OrchestrationBindingContent,
    TemporalUnavailable,
    ensure_workflow,
)
from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _ready_project, _register_worker, _start_goal


def test_legacy_goal_still_claims_plan(api, objects):
    client, token, _auth, goal, plan = _start_goal(api, objects)
    assert goal.get("orchestration_backend", "LEGACY") == "LEGACY"
    assert goal.get("owner_epoch", "1") == "1"

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
    data = claimed.json()["data"]
    lease = data["lease"]
    assert lease is not None
    assert lease["activity_id"] == plan["id"]
    assert data["activity"]["goal_id"] == goal["id"]


def test_legacy_start_command_remains_succeeded(api, objects):
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    commands = client.get(
        "/api/v1/commands",
        params={"goal_id": goal["id"]},
        headers=auth,
    )
    assert commands.status_code == 200, commands.text
    start = next(c for c in commands.json()["data"] if c["kind"] == "START")
    assert start["status"] == "SUCCEEDED"


def test_temporal_goal_skipped_by_global_claim(api, objects):
    client, token, _auth, goal, plan = _start_goal(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1")
        backend = engine.connect().execute(
            text("SELECT orchestration_backend FROM goals WHERE id=:id"),
            {"id": goal["id"]},
        ).scalar_one()
        assert backend == "TEMPORAL"

        ready = engine.connect().execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": plan["id"]},
        ).scalar_one()
        assert ready == "READY"

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

        still_ready = engine.connect().execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": plan["id"]},
        ).scalar_one()
        assert still_ready == "READY"
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def _start_temporal_goal(api, objects):
    """DRAFT → 翻转 TEMPORAL → START；返回 ACCEPTED 命令与 READY PLAN。"""
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
    set_goal_orchestration_backend(engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1")
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    command = started.json()["data"]
    plan = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    ).json()["data"][0]
    return client, token, auth, goal, plan, command, engine


def test_temporal_start_enqueues_pending_delivery(api, objects):
    client, token, _auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    try:
        assert command["status"] == "ACCEPTED"
        assert command["result"]["goal_id"] == goal["id"]
        assert command["result"]["final_status"] == "PLANNING"
        assert plan["status"] == "READY"

        with engine.connect() as db:
            binding = db.execute(
                text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).mappings().one()
            assert binding["backend"] == "TEMPORAL"
            assert binding["workflow_id"] == f"goal-{goal['id']}"
            assert binding["active_run_id"] is None

            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery is not None
            assert delivery["delivery_status"] == "PENDING"
            assert delivery["event_kind"] == "ENSURE_WORKFLOW"
            assert delivery["workflow_id"] == binding["workflow_id"]
            assert delivery["run_id"] is None

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
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_relay_ensure_workflow_acks_and_is_idempotent(api, objects):
    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        with engine.connect() as db:
            binding_row = db.execute(
                text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).mappings().one()

        binding = OrchestrationBindingContent(
            project_id=binding_row["project_id"],
            goal_id=binding_row["goal_id"],
            budget_scope_id=binding_row["budget_scope_id"],
            backend=binding_row["backend"],
            owner_epoch=binding_row["owner_epoch"],
            namespace=binding_row["namespace"],
            workflow_id=binding_row["workflow_id"],
            active_run_id=binding_row["active_run_id"],
            worker_build_id=binding_row["worker_build_id"],
            contract_digest=binding_row["contract_digest"],
        )
        fake = FakeTemporalClient()
        receipt = ensure_workflow(engine, binding, UUID(command["id"]), fake)
        assert receipt.delivery_status == "ACKNOWLEDGED"
        assert receipt.run_id is not None
        assert receipt.workflow_id == binding.workflow_id

        with engine.connect() as db:
            delivery = get_delivery_for_command(db, UUID(command["id"]))
            assert delivery["delivery_status"] == "ACKNOWLEDGED"
            assert delivery["run_id"] == receipt.run_id
            active = db.execute(
                text("SELECT active_run_id FROM orchestration_bindings WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).scalar_one()
            assert active == receipt.run_id
            status = db.execute(
                text("SELECT status FROM command_operations WHERE id=:id"),
                {"id": command["id"]},
            ).scalar_one()
            assert status == "SUCCEEDED"

        again = ensure_workflow(engine, binding, UUID(command["id"]), fake)
        assert again.model_dump() == receipt.model_dump()
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_relay_unavailable_leaves_pending(api, objects):
    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(api, objects)
    try:
        with engine.connect() as db:
            binding_row = db.execute(
                text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).mappings().one()
        binding = OrchestrationBindingContent(
            project_id=binding_row["project_id"],
            goal_id=binding_row["goal_id"],
            budget_scope_id=binding_row["budget_scope_id"],
            backend=binding_row["backend"],
            owner_epoch=binding_row["owner_epoch"],
            namespace=binding_row["namespace"],
            workflow_id=binding_row["workflow_id"],
            active_run_id=binding_row["active_run_id"],
            worker_build_id=binding_row["worker_build_id"],
            contract_digest=binding_row["contract_digest"],
        )
        fake = FakeTemporalClient()
        fake.unavailable = True
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
            status = db.execute(
                text("SELECT status FROM command_operations WHERE id=:id"),
                {"id": command["id"]},
            ).scalar_one()
            assert status == "ACCEPTED"
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_admit_runtime_attempt_on_temporal_plan(api, objects):
    client, token, _auth, goal, plan, _command, engine = _start_temporal_goal(api, objects)
    try:
        subject = str(uuid4())
        _register_worker(subject)
        lease = admit_runtime_attempt(
            engine,
            subject,
            str(uuid4()),
            UUID(plan["id"]),
        )
        assert lease.lease is not None
        assert lease.lease.activity_id == UUID(plan["id"])
        assert lease.lease.fencing_epoch == "1"
        assert lease.activity is not None
        assert lease.activity.status == "RUNNING"
        assert lease.attempt is not None
        assert lease.attempt.status == "ACTIVE"
    finally:
        # admit 已把活动置 RUNNING；须先排空 ACTIVE 再回写 LEGACY
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_temporal_actions_http_admit_and_claim_empty(api, objects):
    """TEMPORAL start → relay ACK → GET actions 含 PLAN → POST admit → 全局 claim 仍空。"""
    client, token, _auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    try:
        with engine.connect() as db:
            binding_row = db.execute(
                text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).mappings().one()
        binding = OrchestrationBindingContent(
            project_id=binding_row["project_id"],
            goal_id=binding_row["goal_id"],
            budget_scope_id=binding_row["budget_scope_id"],
            backend=binding_row["backend"],
            owner_epoch=binding_row["owner_epoch"],
            namespace=binding_row["namespace"],
            workflow_id=binding_row["workflow_id"],
            active_run_id=binding_row["active_run_id"],
            worker_build_id=binding_row["worker_build_id"],
            contract_digest=binding_row["contract_digest"],
        )
        fake = FakeTemporalClient()
        receipt = ensure_workflow(engine, binding, UUID(command["id"]), fake)
        assert receipt.delivery_status == "ACKNOWLEDGED"

        subject = str(uuid4())
        _register_worker(subject)
        worker_auth = {
            "Authorization": "Bearer " + token(subject, ["worker"]),
        }
        actions = client.get(
            "/internal/v1/runtime/actions",
            params={"goal_id": goal["id"], "owner_epoch": "1"},
            headers=worker_auth,
        )
        assert actions.status_code == 200, actions.text
        body = actions.json()["data"]
        assert body["wait_hint"] is None
        assert len(body["actions"]) == 1
        ref = body["actions"][0]
        assert ref["activity_id"] == plan["id"]
        assert ref["action_id"] == plan["id"]
        assert ref["goal_id"] == goal["id"]
        assert ref["owner_epoch"] == "1"
        assert ref["binding_digest"].startswith("sha256:")

        admitted = client.post(
            "/internal/v1/runtime/admit",
            json={"activity_id": plan["id"]},
            headers={**worker_auth, "Idempotency-Key": str(uuid4())},
        )
        assert admitted.status_code == 200, admitted.text
        lease = admitted.json()["data"]
        assert lease["lease"]["activity_id"] == plan["id"]
        assert lease["activity"]["status"] == "RUNNING"

        claimed = client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={**worker_auth, "Idempotency-Key": str(uuid4())},
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["data"]["lease"] is None
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_kernel_activities_ensure_list_admit_against_db(api, objects, monkeypatch):
    """Activity 函数直调（非完整 Worker 进程）：ensure → list → admit PLAN。"""
    from orchestration.kernel_activities import (
        admit_execute_action,
        admit_plan_action,
        configure_kernel_activity_ports,
        ensure_goal_delivery,
        list_runtime_actions,
    )

    client, token, _auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    subject = str(uuid4())
    _register_worker(subject)
    monkeypatch.setenv("RING_WORKFLOW_WORKER_SUBJECT", subject)
    fake = FakeTemporalClient()
    configure_kernel_activity_ports(engine=engine, temporal_client=fake)
    try:
        delivery = ensure_goal_delivery(command["id"], goal["id"])
        assert delivery["delivery_status"] == "ACKNOWLEDGED"
        assert delivery["command_id"] == command["id"]
        assert delivery["run_id"] is not None

        # 幂等
        again = ensure_goal_delivery(command["id"], goal["id"])
        assert again == delivery

        actions = list_runtime_actions(goal["id"], "1")
        assert actions["wait_hint"] is None
        assert len(actions["actions"]) == 1
        assert actions["actions"][0]["activity_id"] == plan["id"]
        assert actions["actions"][0]["kind"] == "PLAN"

        lease = admit_plan_action(plan["id"], f"goal-wf-admit:{plan['id']}")
        assert lease["activity_id"] == plan["id"]
        assert lease["attempt_id"] is not None
        assert lease["fencing_epoch"] == "1"

        # 幂等 admit
        lease2 = admit_plan_action(plan["id"], f"goal-wf-admit:{plan['id']}")
        assert lease2 == lease

        # EXECUTE 入口拒绝 PLAN
        with pytest.raises(PermissionError, match="EXECUTE"):
            admit_execute_action(plan["id"], f"goal-wf-admit-exec:{plan['id']}")
    finally:
        configure_kernel_activity_ports(engine=None, temporal_client=None)
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_ensure_goal_delivery_passes_critic_start_knobs(api, objects, monkeypatch):
    """第三百零二批：ensure_goal_delivery 必须把合同派生 Critic 旋钮写入 ensure_started。

    禁止「有实现无写入方」——仅 unit 公式绿不够，须经 Activity 直调到 Fake 客户端。
    """
    from control_kernel.storage.audits import goal_review_workflow_start_knobs
    from orchestration.kernel_activities import (
        configure_kernel_activity_ports,
        ensure_goal_delivery,
    )

    class _CapturingFake(FakeTemporalClient):
        def __init__(self) -> None:
            super().__init__()
            self.last_kwargs: dict | None = None

        def ensure_started(self, workflow_id: str, **kwargs):  # type: ignore[override]
            self.last_kwargs = dict(kwargs)
            return super().ensure_started(workflow_id, **kwargs)

    client, token, _auth, goal, _plan, command, engine = _start_temporal_goal(
        api, objects
    )
    subject = str(uuid4())
    _register_worker(subject)
    monkeypatch.setenv("RING_WORKFLOW_WORKER_SUBJECT", subject)
    fake = _CapturingFake()
    configure_kernel_activity_ports(engine=engine, temporal_client=fake)
    try:
        expected = goal_review_workflow_start_knobs(goal["contract"])
        assert expected["max_goal_reviews"] == 10
        assert expected["goal_review_interval_seconds"] == 180

        delivery = ensure_goal_delivery(command["id"], goal["id"])
        assert delivery["delivery_status"] == "ACKNOWLEDGED"
        assert fake.last_kwargs is not None
        assert (
            fake.last_kwargs["goal_review_interval_seconds"]
            == expected["goal_review_interval_seconds"]
        )
        assert fake.last_kwargs["max_goal_reviews"] == expected["max_goal_reviews"]
        # 透传不得冒充 Goal DONE
        assert fake.last_kwargs.get("marks_goal_done") in (None, False)
    finally:
        configure_kernel_activity_ports(engine=None, temporal_client=None)
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()


def test_observe_activity_status_after_admit(api, objects, monkeypatch):
    """TEMPORAL start→relay→admit→SQL 置 SUCCEEDED→observe_activity_status（观察≠验收）。"""
    from orchestration.kernel_activities import (
        admit_plan_action,
        configure_kernel_activity_ports,
        ensure_goal_delivery,
        is_activity_terminal_status,
        observe_activity_status,
        observe_carried_plan_activity_status,
        read_activity_status,
    )

    client, token, _auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    subject = str(uuid4())
    _register_worker(subject)
    monkeypatch.setenv("RING_WORKFLOW_WORKER_SUBJECT", subject)
    fake = FakeTemporalClient()
    configure_kernel_activity_ports(engine=engine, temporal_client=fake)
    try:
        delivery = ensure_goal_delivery(command["id"], goal["id"])
        assert delivery["delivery_status"] == "ACKNOWLEDGED"

        lease = admit_plan_action(plan["id"], f"goal-wf-admit:{plan['id']}")
        assert lease["activity_id"] == plan["id"]

        running = observe_activity_status(plan["id"])
        assert running["activity_id"] == plan["id"]
        assert running["kind"] == "PLAN"
        assert running["status"] == "RUNNING"
        assert not is_activity_terminal_status(running["status"])

        carried = observe_carried_plan_activity_status(goal["id"], "1", plan["id"])
        assert carried == running
        with pytest.raises(PermissionError, match="绑定不匹配"):
            observe_carried_plan_activity_status(str(uuid4()), "1", plan["id"])
        with pytest.raises(PermissionError, match="绑定不匹配"):
            observe_carried_plan_activity_status(goal["id"], "2", plan["id"])

        # 观察-only：直接写终态，不走 outcome/验收路径
        with engine.begin() as db:
            db.execute(
                text("UPDATE activities SET status='SUCCEEDED' WHERE id=:id"),
                {"id": plan["id"]},
            )

        observed = observe_activity_status(plan["id"])
        assert observed == {
            "activity_id": plan["id"],
            "status": "SUCCEEDED",
            "kind": "PLAN",
        }
        assert is_activity_terminal_status(observed["status"])
        # helper 与 Activity 同形
        assert read_activity_status(engine, plan["id"]) == observed

        # Goal 仍非 DONE（观察不等于完成判定）
        goal_status = engine.connect().execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal["id"]},
        ).scalar_one()
        assert goal_status != "DONE"
    finally:
        configure_kernel_activity_ports(engine=None, temporal_client=None)
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()


def test_require_temporal_rejects_legacy_start(api, objects, monkeypatch):
    """REQUIRE_TEMPORAL + TARGET：禁止 START 仍为 LEGACY 的 Goal。"""
    monkeypatch.setenv("RING_ORCHESTRATION_REQUIRE_TEMPORAL", "1")
    monkeypatch.setenv("RING_TEMPORAL_TARGET", "127.0.0.1:7233")

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    _drain_ready_plans(client, token)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    assert goal.get("orchestration_backend") == "TEMPORAL"

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        # 强制回 LEGACY 再测拒绝 START（存量排空路径）
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        started = client.post(
            f"/api/v1/goals/{goal['id']}/start",
            json={"expected_state_revision": 1, "reason": "plan"},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert started.status_code == 409, started.text
        err = started.json()["error"]
        assert err["code"] == "LEGACY_ORCHESTRATION_FORBIDDEN"
    finally:
        _drain_ready_plans(client, token)
        engine.dispose()


def test_require_temporal_blocks_global_claim_without_drain(api, objects, monkeypatch):
    """强制 TEMPORAL 时全局 claim 不领取 LEGACY Goal；开 DRAIN 后可排空。"""
    client, token, _auth, goal, plan = _start_goal(api, objects)
    assert goal.get("orchestration_backend", "LEGACY") == "LEGACY"

    monkeypatch.setenv("RING_ORCHESTRATION_REQUIRE_TEMPORAL", "1")
    monkeypatch.setenv("RING_TEMPORAL_TARGET", "127.0.0.1:7233")
    monkeypatch.delenv("RING_LEGACY_CLAIM_DRAIN", raising=False)

    subject = str(uuid4())
    _register_worker(subject)
    blocked = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(subject, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["data"]["lease"] is None

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        still_ready = engine.connect().execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": plan["id"]},
        ).scalar_one()
        assert still_ready == "READY"

        monkeypatch.setenv("RING_LEGACY_CLAIM_DRAIN", "1")
        drained = client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={
                "Authorization": "Bearer " + token(subject, ["worker"]),
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert drained.status_code == 200, drained.text
        lease = drained.json()["data"]["lease"]
        assert lease is not None
        assert lease["activity_id"] == plan["id"]
    finally:
        monkeypatch.delenv("RING_LEGACY_CLAIM_DRAIN", raising=False)
        monkeypatch.delenv("RING_ORCHESTRATION_REQUIRE_TEMPORAL", raising=False)
        monkeypatch.delenv("RING_TEMPORAL_TARGET", raising=False)
        _drain_ready_plans(client, token)
        engine.dispose()


def test_admit_rejects_execute_while_goal_pausing(api, objects):
    """PAUSING 时具名 admit EXECUTE 必须失败关闭（不得新开 ENGINEERING 租约）。"""
    from control_kernel.protocols.runtime import LeaseRejected
    from test_replan import _publish_running

    client, token, auth, goal = _publish_running(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        executes = client.get(
            f"/api/v1/goals/{goal['id']}/activities",
            params={"kind": "EXECUTE", "status": "READY"},
            headers=auth,
        )
        assert executes.status_code == 200, executes.text
        rows = executes.json()["data"]
        assert len(rows) >= 1
        execute_id = rows[0]["id"]

        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1"
        )
        with engine.begin() as db:
            db.execute(
                text(
                    """UPDATE goals SET status='PAUSING', updated_at=clock_timestamp()
                    WHERE id=:id"""
                ),
                {"id": goal["id"]},
            )
            ready = db.execute(
                text("SELECT status FROM activities WHERE id=:id"),
                {"id": execute_id},
            ).scalar_one()
            assert ready == "READY"

        subject = str(uuid4())
        _register_worker(subject, kinds=("EXECUTE", "PLAN"))
        with pytest.raises(LeaseRejected) as exc:
            admit_runtime_attempt(
                engine,
                subject,
                str(uuid4()),
                UUID(execute_id),
            )
        assert exc.value.code == "INVALID_STATE"
        assert "PAUSING" in exc.value.message
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()


def test_admit_rejects_execute_while_goal_cancelling(api, objects):
    """CANCELLING 时具名 admit EXECUTE 必须失败关闭。"""
    from control_kernel.protocols.runtime import LeaseRejected
    from test_replan import _publish_running

    client, token, auth, goal = _publish_running(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        execute_id = client.get(
            f"/api/v1/goals/{goal['id']}/activities",
            params={"kind": "EXECUTE", "status": "READY"},
            headers=auth,
        ).json()["data"][0]["id"]

        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1"
        )
        with engine.begin() as db:
            db.execute(
                text(
                    """UPDATE goals SET status='CANCELLING', updated_at=clock_timestamp()
                    WHERE id=:id"""
                ),
                {"id": goal["id"]},
            )

        subject = str(uuid4())
        _register_worker(subject, kinds=("EXECUTE", "PLAN"))
        with pytest.raises(LeaseRejected) as exc:
            admit_runtime_attempt(
                engine,
                subject,
                str(uuid4()),
                UUID(execute_id),
            )
        assert "CANCELLING" in exc.value.message
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()


def test_runtime_actions_empty_while_goal_pausing(api, objects):
    """PAUSING 时 runtime actions 不下发 READY EXECUTE，wait_hint=GOAL_CONTROL_DRAINING。"""
    from control_kernel.storage.orchestration import get_runtime_actions
    from test_replan import _publish_running

    client, token, auth, goal = _publish_running(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        executes = client.get(
            f"/api/v1/goals/{goal['id']}/activities",
            params={"kind": "EXECUTE", "status": "READY"},
            headers=auth,
        ).json()["data"]
        assert len(executes) >= 1
        execute = executes[0]

        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "TEMPORAL", owner_epoch="1"
        )
        with engine.begin() as db:
            db.execute(
                text(
                    """UPDATE goals SET status='PAUSING', updated_at=clock_timestamp()
                    WHERE id=:id"""
                ),
                {"id": goal["id"]},
            )
            exists = db.execute(
                text("SELECT 1 FROM orchestration_bindings WHERE goal_id=:g"),
                {"g": goal["id"]},
            ).first()
            if exists is None:
                budget_scope = db.execute(
                    text("SELECT budget_scope_id FROM activities WHERE id=:id"),
                    {"id": execute["id"]},
                ).scalar_one()
                db.execute(
                    text(
                        """INSERT INTO orchestration_bindings(
                          id, project_id, goal_id, budget_scope_id, backend, owner_epoch,
                          namespace, workflow_id, active_run_id, worker_build_id,
                          contract_digest)
                        VALUES (
                          :id, :project, :goal, :budget, 'TEMPORAL', '1',
                          'default', :wf, :run, 'm0-py-1.32.0-dev',
                          :digest)"""
                    ),
                    {
                        "id": uuid4(),
                        "project": goal["project_id"],
                        "goal": goal["id"],
                        "budget": budget_scope,
                        "wf": f"goal-{goal['id']}",
                        "run": str(uuid4()),
                        "digest": "sha256:" + "e" * 64,
                    },
                )

        subject = str(uuid4())
        _register_worker(subject, kinds=("EXECUTE", "PLAN"))
        result = get_runtime_actions(
            engine,
            subject,
            goal_id=UUID(goal["id"]),
            expected_owner_epoch="1",
            project_ids=[],
        )
        assert result.actions == []
        assert result.wait_hint is not None
        assert result.wait_hint.code == "GOAL_CONTROL_DRAINING"
        assert "PAUSING" in result.wait_hint.message
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
