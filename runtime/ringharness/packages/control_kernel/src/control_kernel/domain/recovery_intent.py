"""恢复意图闸门（AB04）：过期 / schema / 次数 / 开关 → 可观测放弃，绝不 DONE。

与 Temporal GoalWorkflow 门3 裁决顺序对齐，供 Kernel 侧单测与后续 Workflow 收敛调用。
本模块无 IO；编排面接线仍归 Hermes（packages/orchestration）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

RecoveryAbandonReason = Literal[
    "INTENT_EXPIRED",
    "CHECKPOINT_SCHEMA_INCOMPATIBLE",
    "RECOVERY_ATTEMPTS_EXCEEDED",
    "RECOVERY_DISABLED",
]

# 与 orchestration.temporal_workflows._RECOVERY_CHECKPOINT_SCHEMA_VERSION 对齐
RECOVERY_CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RecoveryAbandon:
    reason: RecoveryAbandonReason
    marks_goal_done: Literal[False] = False


@dataclass(frozen=True, slots=True)
class RecoveryProceed:
    marks_goal_done: Literal[False] = False


RecoveryDecision = RecoveryAbandon | RecoveryProceed


def parse_intent_deadline(raw: str | None) -> datetime | None:
    """解析 intent_valid_until；非法或无时区 → None（调用方映射为 schema 不兼容）。"""
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def evaluate_generation_recovery(
    *,
    generation: int,
    recovery_enabled: bool,
    checkpoint_schema_version: int | None,
    intent_valid_until: datetime | str | None,
    recovery_attempts: int,
    max_recovery_attempts: int,
    now: datetime,
    expected_schema_version: int = RECOVERY_CHECKPOINT_SCHEMA_VERSION,
) -> RecoveryDecision:
    """generation>0 时的恢复安全裁决；generation≤0 直接续跑（无恢复闸）。

    顺序与 GoalWorkflow 门3 一致：开关 → schema → 次数 → 意图截止。
    """
    if generation <= 0:
        return RecoveryProceed()

    if not recovery_enabled:
        return RecoveryAbandon(reason="RECOVERY_DISABLED")

    if (
        checkpoint_schema_version is None
        or int(checkpoint_schema_version) != expected_schema_version
    ):
        return RecoveryAbandon(reason="CHECKPOINT_SCHEMA_INCOMPATIBLE")

    if recovery_attempts > max_recovery_attempts:
        return RecoveryAbandon(reason="RECOVERY_ATTEMPTS_EXCEEDED")

    if isinstance(intent_valid_until, datetime):
        deadline = intent_valid_until
        if deadline.tzinfo is None:
            return RecoveryAbandon(reason="CHECKPOINT_SCHEMA_INCOMPATIBLE")
    else:
        deadline = parse_intent_deadline(intent_valid_until)
        if deadline is None:
            return RecoveryAbandon(reason="CHECKPOINT_SCHEMA_INCOMPATIBLE")

    if now.tzinfo is None:
        raise ValueError("now 必须带时区")

    if now > deadline:
        return RecoveryAbandon(reason="INTENT_EXPIRED")

    return RecoveryProceed()
