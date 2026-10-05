"""PlanStaticValidator 纯领域单测：六类失败关闭 + 合法 DAG。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from control_kernel.domain.plan_static_validator import (
    PlanStaticViolation,
    validate_plan_static,
)
from control_kernel.protocols.goals import Budget
from control_kernel.protocols.plans import PlanCreate
from pydantic import ValidationError

PROFILE = "11111111-1111-1111-1111-111111111111"


def _budget(**over) -> dict:
    base = {
        "wall_clock_seconds": 3600,
        "max_tokens": 100000,
        "max_cost_usd": "10",
        "max_tool_calls": 100,
        "max_network_calls": 100,
        "max_disk_bytes": 1048576,
        "max_gpu_seconds": None,
    }
    base.update(over)
    return base


def _task(
    tid,
    *,
    depends_on=None,
    deliverables=None,
    allowed_paths=None,
    protected_paths=None,
    budget=None,
    acceptance_id="A1",
    covers=None,
    required_acceptance=True,
):
    return {
        "id": str(tid),
        "contract": {
            "objective": "do work",
            "depends_on": [str(d) for d in (depends_on or [])],
            "input_artifact_ids": [],
            "deliverables": deliverables
            if deliverables is not None
            else [{"kind": "patch", "required": True}],
            "acceptance": [
                {
                    "id": acceptance_id,
                    "description": "ok",
                    "required": required_acceptance,
                    "verification_profile_id": PROFILE,
                }
            ],
            "covers_goal_criterion_ids": covers or ["G1"],
            "allowed_paths": allowed_paths if allowed_paths is not None else ["src/**"],
            "protected_paths": protected_paths if protected_paths is not None else [],
            "required_capabilities": [],
            "budget": budget or _budget(),
            "retry_policy": {
                "max_execution_rounds": 2,
                "max_audit_attempts_per_candidate": 2,
                "max_activity_retries": 1,
            },
            "resources": {
                "cpu_millicores": 100,
                "memory_bytes": 268435456,
                "disk_bytes": 67108864,
                "model_slots": 0,
                "browser_slots": 0,
                "exclusive_labels": [],
            },
            "risk": "low",
        },
        "replaces_task_id": None,
    }


def _plan(tasks, coverage):
    return PlanCreate.model_validate(
        {
            "expected_plan_revision": None,
            "reason": "test",
            "tasks": tasks,
            "coverage": coverage,
        }
    )


def _run(body: PlanCreate, goal_budget=None):
    validate_plan_static(
        body,
        goal_budget=Budget.model_validate(goal_budget or _budget()),
        goal_criterion_ids={"G1"},
        required_goal_criterion_ids={"G1"},
    )


def test_合法_dag_通过():
    a, b = uuid4(), uuid4()
    body = _plan(
        [
            _task(a, allowed_paths=["a/**"]),
            _task(b, depends_on=[a], allowed_paths=["b/**"]),
        ],
        [
            {
                "goal_criterion_id": "G1",
                "task_id": str(b),
                "task_acceptance_id": "A1",
                "verification_profile_id": PROFILE,
            }
        ],
    )
    _run(body)


def test_环_失败():
    a, b = uuid4(), uuid4()
    body = _plan(
        [
            _task(a, depends_on=[b], allowed_paths=["a/**"]),
            _task(b, depends_on=[a], allowed_paths=["b/**"]),
        ],
        [
            {
                "goal_criterion_id": "G1",
                "task_id": str(a),
                "task_acceptance_id": "A1",
                "verification_profile_id": PROFILE,
            }
        ],
    )
    with pytest.raises(PlanStaticViolation) as ei:
        _run(body)
    assert ei.value.code == "PLAN_CYCLE"


def test_悬空依赖_由_PlanCreate_schema_拒绝():
    a, missing = uuid4(), uuid4()
    with pytest.raises(ValidationError):
        _plan(
            [_task(a, depends_on=[missing])],
            [
                {
                    "goal_criterion_id": "G1",
                    "task_id": str(a),
                    "task_acceptance_id": "A1",
                    "verification_profile_id": PROFILE,
                }
            ],
        )


def test_无_producer_失败():
    a, b = uuid4(), uuid4()
    body = _plan(
        [
            _task(a, deliverables=[], allowed_paths=["a/**"]),
            _task(b, depends_on=[a], allowed_paths=["b/**"]),
        ],
        [
            {
                "goal_criterion_id": "G1",
                "task_id": str(b),
                "task_acceptance_id": "A1",
                "verification_profile_id": PROFILE,
            }
        ],
    )
    with pytest.raises(PlanStaticViolation) as ei:
        _run(body)
    assert ei.value.code == "PLAN_MISSING_PRODUCER"


def test_路径边界冲突_任务内():
    a = uuid4()
    body = _plan(
        [_task(a, allowed_paths=["src/x"], protected_paths=["src/x"])],
        [
            {
                "goal_criterion_id": "G1",
                "task_id": str(a),
                "task_acceptance_id": "A1",
                "verification_profile_id": PROFILE,
            }
        ],
    )
    with pytest.raises(PlanStaticViolation) as ei:
        _run(body)
    assert ei.value.code == "PLAN_PATH_BOUNDARY_CONFLICT"


def test_路径边界冲突_无依赖共享():
    a, b = uuid4(), uuid4()
    body = _plan(
        [
            _task(a, allowed_paths=["shared/path"]),
            _task(b, allowed_paths=["shared/path"]),
        ],
        [
            {
                "goal_criterion_id": "G1",
                "task_id": str(a),
                "task_acceptance_id": "A1",
                "verification_profile_id": PROFILE,
            }
        ],
    )
    with pytest.raises(PlanStaticViolation) as ei:
        _run(body)
    assert ei.value.code == "PLAN_PATH_BOUNDARY_CONFLICT"


def test_criterion_无验证路径():
    a = uuid4()
    body = _plan(
        [_task(a, covers=["G1"])],
        [
            {
                "goal_criterion_id": "G1",
                "task_id": str(a),
                "task_acceptance_id": "A1",
                "verification_profile_id": PROFILE,
            }
        ],
    )
    # 清空 coverage → 必要目标标准无验证路径
    raw = body.model_dump(mode="json")
    raw["coverage"] = [
        {
            "goal_criterion_id": "G1",
            "task_id": str(a),
            "task_acceptance_id": "MISSING",
            "verification_profile_id": PROFILE,
        }
    ]
    body2 = PlanCreate.model_validate(raw)
    with pytest.raises(PlanStaticViolation) as ei:
        _run(body2)
    assert ei.value.code == "PLAN_CRITERION_NO_VERIFICATION_PATH"


def test_预算不可能():
    a = uuid4()
    body = _plan(
        [_task(a, budget=_budget(wall_clock_seconds=99999))],
        [
            {
                "goal_criterion_id": "G1",
                "task_id": str(a),
                "task_acceptance_id": "A1",
                "verification_profile_id": PROFILE,
            }
        ],
    )
    with pytest.raises(PlanStaticViolation) as ei:
        _run(body, goal_budget=_budget(wall_clock_seconds=3600))
    assert ei.value.code == "PLAN_BUDGET_IMPOSSIBLE"
    assert "wall_clock_seconds" in ei.value.message
