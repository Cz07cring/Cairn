"""M3.5：activation 终止原因域裁决；恒 ≠ Goal DONE。"""

from __future__ import annotations

from control_kernel.domain.activation_termination import (
    ACTIVATION_TERMINATION_REASONS,
    parse_activation_termination_reason,
)


def test_parse_four_reasons_marks_goal_done_false():
    for reason in sorted(ACTIVATION_TERMINATION_REASONS):
        got = parse_activation_termination_reason(reason)
        assert got.reason == reason
        assert got.marks_goal_done is False
