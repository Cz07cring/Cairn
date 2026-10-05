"""Pure deterministic metric evaluation. Never establishes provenance or marks a Goal DONE."""

import hashlib
import re
from decimal import Decimal
from typing import Literal, TypedDict

from ..protocols.goals import Criterion
from ..protocols.verification import (
    MetricObservation,
    VerificationProfileCreate,
    VerifierDefinition,
)

Verdict = Literal["PASS", "INSUFFICIENT", "FAIL"]

# Kernel Assessment 绑定的判定算法版本；禁止客户端传入。
EVALUATOR_DIGEST = (
    "sha256:"
    + hashlib.sha256(b"control_kernel.domain.verification.evaluate_metrics:v1").hexdigest()
)


class MetricEvaluation(TypedDict):
    verdict: Verdict
    criterion_results: dict[str, Verdict]


def parse_metric(value: str, kind: str) -> int | Decimal | bool | str:
    if kind == "INTEGER":
        if not re.fullmatch(r"(0|-?[1-9][0-9]*)", value):
            raise ValueError("noncanonical integer")
        number = int(value)
        if abs(number) > 9007199254740991:
            raise ValueError("unsafe integer")
        return number
    if kind == "DECIMAL":
        if len(value) > 32 or not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]{0,5}[1-9])?", value):
            raise ValueError("noncanonical decimal")
        return Decimal(value)
    if kind == "BOOLEAN":
        if value not in {"true", "false"}:
            raise ValueError("invalid boolean")
        return value == "true"
    if kind == "STRING":
        value.encode("utf-8", errors="strict")
        return value
    raise ValueError("unknown metric type")


def validate_profile(definition: VerifierDefinition, profile: VerificationProfileCreate) -> None:
    if definition.project_id != profile.project_id or profile.required_layers != [definition.layer]:
        raise ValueError("definition project/layer mismatch")
    if profile.applicability_rule_ref != "all_required":
        raise ValueError("applicability rule is not registered")
    metrics = {m.metric: m for m in definition.metric_definitions}
    for t in profile.thresholds:
        m = metrics.get(t.metric)
        if m is None or m.unit != t.unit:
            raise ValueError("metric/unit not declared")
        if t.operator != "EQ" and m.value_type not in {"INTEGER", "DECIMAL"}:
            raise ValueError("ordering requires numeric metric")
        parse_metric(t.expected, m.value_type)
    if definition.layer in {"SEMANTIC", "ADVERSARIAL"}:
        score = [t for t in profile.thresholds if t.metric == "score_bp" and t.operator == "GE"]
        critical = [
            t
            for t in profile.thresholds
            if t.metric == "critical_violation" and t.operator == "EQ" and t.expected == "false"
        ]
        if not score or not critical or any(not 0 <= int(t.expected) <= 10000 for t in score):
            raise ValueError("semantic thresholds missing or out of range")


def evaluate_metrics(
    definition: dict, profile: dict, criteria: list[dict], observations: list[dict]
) -> MetricEvaluation:
    """Requires callers to verify binding and source trust before accepting this assessment."""
    d = VerifierDefinition.model_validate(definition)
    p = VerificationProfileCreate.model_validate(profile)
    validate_profile(d, p)
    cs = [Criterion.model_validate(c) for c in criteria]
    if not cs or not any(c.required for c in cs) or len({c.id for c in cs}) != len(cs):
        raise ValueError("invalid criteria")
    metrics = {m.metric: m for m in d.metric_definitions}
    seen = {}
    for raw in observations:
        o = MetricObservation.model_validate(raw)
        key = (o.criterion_id, o.metric)
        if key in seen or o.criterion_id not in {c.id for c in cs} or o.metric not in metrics:
            raise ValueError("duplicate or unbound observation")
        if o.status == "OBSERVED":
            parse_metric(o.value, metrics[o.metric].value_type)
            if (
                d.layer in {"SEMANTIC", "ADVERSARIAL"}
                and o.metric == "score_bp"
                and not 0 <= int(o.value) <= 10000
            ):
                raise ValueError("score outside basis point range")
        seen[key] = o
    outcomes = {}
    for c in cs:
        verdict: Verdict = "PASS"
        for t in p.thresholds:
            o = seen.get((c.id, t.metric))
            if o is None or o.status != "OBSERVED":
                if verdict != "FAIL":
                    verdict = "INSUFFICIENT"
                continue
            actual = parse_metric(o.value, metrics[t.metric].value_type)
            expected = parse_metric(t.expected, metrics[t.metric].value_type)
            passed = (
                (actual == expected)
                if t.operator == "EQ"
                else (actual <= expected if t.operator == "LE" else actual >= expected)
            )
            if not passed:
                verdict = "FAIL"
        outcomes[c.id] = verdict
    required = [outcomes[c.id] for c in cs if c.required]
    total: Verdict = (
        "FAIL" if "FAIL" in required else ("INSUFFICIENT" if "INSUFFICIENT" in required else "PASS")
    )
    if d.layer in {"SEMANTIC", "ADVERSARIAL"} and any(
        o.metric == "critical_violation" and o.status == "OBSERVED" and o.value == "true"
        for o in seen.values()
    ):
        total = "FAIL"
    return {"verdict": total, "criterion_results": outcomes}
