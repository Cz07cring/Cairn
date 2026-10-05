"""ContextCompiler 纯函数单测。"""

from uuid import uuid4

import pytest
from context_compiler import (
    ActivitySnapshot,
    CompileRejected,
    InputArtifactRef,
    TokenBudget,
    compile_context_bundle,
)
from context_compiler.snapshot import role_for_kind, skill_allowed_for_role


def test_role_mapping_and_skill_scope():
    assert role_for_kind("PLAN") == "PLANNER"
    assert role_for_kind("EXECUTE") == "EXECUTOR"
    assert skill_allowed_for_role("PLANNER", ["PLAN"])
    assert not skill_allowed_for_role("PLANNER", ["EXECUTE"])


def test_compile_planner_bundle_with_contract_and_skill():
    project = uuid4()
    goal = uuid4()
    activity = uuid4()
    contract_art = uuid4()
    skill_art = uuid4()
    content = compile_context_bundle(
        ActivitySnapshot(
            project_id=project,
            activity_id=activity,
            role="PLANNER",
            goal_id=goal,
            goal_contract_revision=1,
            task_contract_revision=None,
            plan_revision=None,
            input_bindings=(
                InputArtifactRef(
                    artifact_id=contract_art,
                    digest="sha256:" + "a" * 64,
                    classification="CONTRACT",
                ),
                InputArtifactRef(
                    artifact_id=skill_art,
                    digest="sha256:" + "b" * 64,
                    classification="SKILL",
                ),
            ),
        ),
        TokenBudget(max_input_tokens=4096),
    )
    assert content["role"] == "PLANNER"
    assert content["goal_id"] == str(goal)
    assert len(content["input_bindings"]) == 2
    assert content["planning_feedback_ids"] == []


def test_compile_rejects_feedback_for_non_planner():
    with pytest.raises(CompileRejected):
        compile_context_bundle(
            ActivitySnapshot(
                project_id=uuid4(),
                activity_id=uuid4(),
                role="EXECUTOR",
                goal_id=uuid4(),
                goal_contract_revision=1,
                task_contract_revision=1,
                plan_revision=1,
                planning_feedback_ids=(uuid4(),),
            )
        )


def test_compile_rejects_over_budget():
    bindings = tuple(
        InputArtifactRef(
            artifact_id=uuid4(),
            digest="sha256:" + f"{i:064x}"[:64].ljust(64, "0"),
            classification="SKILL",
        )
        for i in range(20)
    )
    with pytest.raises(CompileRejected, match="token 预算"):
        compile_context_bundle(
            ActivitySnapshot(
                project_id=uuid4(),
                activity_id=uuid4(),
                role="PLANNER",
                goal_id=uuid4(),
                goal_contract_revision=1,
                task_contract_revision=None,
                plan_revision=None,
                input_bindings=bindings,
            ),
            TokenBudget(max_input_tokens=200),
        )
