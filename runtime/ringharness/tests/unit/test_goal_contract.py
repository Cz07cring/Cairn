"""API model acceptance must agree with the frozen GoalContract schema and invariants."""

import json
import re
from pathlib import Path

import pytest
from control_kernel.protocols.goals import GoalCreate
from pydantic import ValidationError

DOC = (Path(__file__).parents[2] / "doc/05-API接口文档.md").read_text()
EXAMPLE = json.loads(
    next(
        block
        for block in re.findall(r"```json\n(.*?)\n```", DOC, re.DOTALL)
        if "success_criteria" in block
    )
)


def test_goal_contract_accepts_frozen_example_and_rejects_unsafe_inputs():
    goal = GoalCreate.model_validate(EXAMPLE)
    assert goal.budget.max_cost_usd == "0"
    assert goal.success_criteria[0].required is True
    invalid = []
    for key, value in [
        ("max_tokens", True),
        ("max_tokens", "10"),
        ("max_cost_usd", "1e3"),
        ("wall_clock_seconds", 0),
        ("max_disk_bytes", 9007199254740992),
    ]:
        invalid.append({**EXAMPLE, "budget": {**EXAMPLE["budget"], key: value}})
    criterion = EXAMPLE["success_criteria"][0]
    invalid.extend(
        [
            {**EXAMPLE, "success_criteria": []},
            {**EXAMPLE, "success_criteria": [criterion, criterion]},
            {**EXAMPLE, "success_criteria": [{**criterion, "required": False}]},
            {**EXAMPLE, "success_criteria": [{**criterion, "required": "true"}]},
            {**EXAMPLE, "budget": {**EXAMPLE["budget"], "unlimited": True}},
            {**EXAMPLE, "objective": ""},
        ]
    )
    for body in invalid:
        with pytest.raises(ValidationError):
            GoalCreate.model_validate(body)
