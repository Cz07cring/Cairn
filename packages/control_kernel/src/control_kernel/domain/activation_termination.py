"""Activation 确定性终止原因（M3.5）：可观测、≠ Goal DONE。

供 Runner/Workflow 后续接线；本模块无 IO。M4 屏障可消费这些原因，
但任何原因都不得被解释为 Audit PASS / Goal DONE。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActivationTerminationReason = Literal[
    "NO_PROGRESS_STOP",
    "BUDGET_EXHAUSTED",
    "CANCELLED",
    "GOAL_REQUIRES_REVIEW",
]

ACTIVATION_TERMINATION_REASONS: frozenset[str] = frozenset(
    {
        "NO_PROGRESS_STOP",
        "BUDGET_EXHAUSTED",
        "CANCELLED",
        "GOAL_REQUIRES_REVIEW",
    }
)


@dataclass(frozen=True, slots=True)
class ActivationTermination:
    """域层终止裁决；恒 marks_goal_done=False。"""

    reason: ActivationTerminationReason
    marks_goal_done: Literal[False] = False


def parse_activation_termination_reason(raw: str) -> ActivationTermination:
    """非法 reason → ValueError（调用方映射为 PlanRejected）。"""
    text = (raw or "").strip()
    if text not in ACTIVATION_TERMINATION_REASONS:
        raise ValueError(f"未知 activation 终止原因: {raw!r}")
    return ActivationTermination(reason=text)  # type: ignore[arg-type]
