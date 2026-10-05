"""GoalWorkflow M1 tick：ensure → actions → admit PLAN；禁止工具。"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from control_kernel.protocols.runtime import (
    RuntimeActionRef,
    RuntimeActionsResult,
)
from orchestration.client import FakeTemporalClient
from orchestration.goal_workflow import GoalWorkflowKernelPorts, run_goal_workflow_m1_tick
from orchestration.protocols import DeliveryReceipt, OrchestrationBindingContent


def test_run_goal_workflow_m1_tick_ensure_then_admit_without_tools(monkeypatch):
    goal_id = uuid4()
    project_id = uuid4()
    activity_id = uuid4()
    command_id = uuid4()
    digest = "sha256:" + "ab" * 32

    binding = OrchestrationBindingContent(
        project_id=project_id,
        goal_id=goal_id,
        budget_scope_id=goal_id,
        backend="TEMPORAL",
        owner_epoch="1",
        namespace="default",
        workflow_id=f"goal-{goal_id}",
        active_run_id=None,
        worker_build_id="m0-py-1.32.0-dev",
        contract_digest=digest,
    )
    receipt = DeliveryReceipt(
        command_id=command_id,
        workflow_id=binding.workflow_id,
        run_id="run-1",
        delivery_status="ACKNOWLEDGED",
    )
    actions = RuntimeActionsResult(
        actions=[
            RuntimeActionRef(
                project_id=project_id,
                goal_id=goal_id,
                activity_id=activity_id,
                action_id=activity_id,
                owner_epoch="1",
                binding_digest=digest,
            )
        ],
        wait_hint=None,
    )
    lease = MagicMock(name="ActivityLease")

    ensure_calls: list[tuple] = []
    admit_calls: list[tuple] = []

    def fake_ensure(engine, bound, cmd, client):
        ensure_calls.append((bound.workflow_id, cmd, client))
        return receipt

    def fake_actions(engine, subject, *, goal_id, expected_owner_epoch, project_ids):
        assert goal_id == binding.goal_id
        assert expected_owner_epoch == "1"
        return actions

    def fake_admit(engine, subject, key, aid, *, lease_ttl=None):
        admit_calls.append((subject, key, aid))
        return lease

    monkeypatch.setattr(
        "orchestration.goal_workflow.ensure_workflow", fake_ensure
    )
    monkeypatch.setattr(
        "orchestration.goal_workflow.get_runtime_actions", fake_actions
    )
    monkeypatch.setattr(
        "orchestration.goal_workflow.admit_runtime_attempt", fake_admit
    )

    engine = MagicMock()
    # kind=PLAN 查询
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.return_value.scalar_one_or_none.return_value = "PLAN"
    engine.connect.return_value = conn

    ports = GoalWorkflowKernelPorts(
        engine=engine,
        temporal_client=FakeTemporalClient(),
        subject="worker-1",
        project_ids=[],
        admit_key_prefix="tick-test",
    )
    result = run_goal_workflow_m1_tick(ports, binding, command_id)

    assert result.delivery == receipt
    assert result.actions == actions
    assert len(result.admitted) == 1
    assert result.admitted[0] is lease
    assert len(ensure_calls) == 1
    assert ensure_calls[0][1] == command_id
    assert len(admit_calls) == 1
    assert admit_calls[0][2] == activity_id
    assert admit_calls[0][1] == f"tick-test:{activity_id}"
