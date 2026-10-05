"""Frozen verifier vectors prove arithmetic only, not provenance or model quality."""

import json
from pathlib import Path

import pytest
from control_kernel.domain.verification import evaluate_metrics

EXAMPLES = json.loads(
    (Path(__file__).parents[2] / "doc/contracts/verifier-fixtures-v2.json").read_text()
)["examples"]
CASES = [(example, case) for example in EXAMPLES for case in example["cases"]]


@pytest.mark.parametrize("example,case", CASES)
def test_frozen_verifier_metric_outcomes(example, case):
    result = evaluate_metrics(
        example["definition"], example["profile"], example["criteria"], case["run"]["observations"]
    )
    assert result["verdict"] == case["expected_verdict"]


def test_invalid_observations_and_critical_violation_cannot_pass():
    from copy import deepcopy

    example = deepcopy(EXAMPLES[1])
    passing = example["cases"][0]["run"]["observations"]
    # An explicit critical violation stays fatal even on an optional reported criterion.
    criteria = example["criteria"] + [
        {**example["criteria"][0], "id": "optional", "required": False}
    ]
    observations = passing + [
        {
            "criterion_id": "optional",
            "metric": "critical_violation",
            "value": "true",
            "status": "OBSERVED",
            "evidence_ids": passing[0]["evidence_ids"],
            "reason_code": "MEASURED",
        }
    ]
    assert (
        evaluate_metrics(example["definition"], example["profile"], criteria, observations)[
            "verdict"
        ]
        == "FAIL"
    )
    for value in ["9000.0", "1e3", "-0", "9007199254740992", "10001"]:
        invalid = deepcopy(passing)
        invalid[0]["value"] = value
        with pytest.raises(ValueError):
            evaluate_metrics(
                example["definition"], example["profile"], example["criteria"], invalid
            )
    invalid = deepcopy(passing)
    invalid[0]["evidence_ids"] = []
    with pytest.raises(ValueError):
        evaluate_metrics(example["definition"], example["profile"], example["criteria"], invalid)


def test_profile_cannot_substitute_metric_type_layer_or_applicability():
    from copy import deepcopy

    example = deepcopy(EXAMPLES[0])
    observations = example["cases"][0]["run"]["observations"]
    for change in [
        {"applicability_rule_ref": "model-decides-what-to-skip"},
        {"required_layers": ["GLOBAL"]},
        {
            "thresholds": [
                {"metric": "checks_passed", "operator": "GE", "expected": "true", "unit": "boolean"}
            ]
        },
        {
            "thresholds": [
                {"metric": "checks_passed", "operator": "EQ", "expected": "true", "unit": "seconds"}
            ]
        },
    ]:
        with pytest.raises(ValueError):
            evaluate_metrics(
                example["definition"],
                {**example["profile"], **change},
                example["criteria"],
                observations,
            )
