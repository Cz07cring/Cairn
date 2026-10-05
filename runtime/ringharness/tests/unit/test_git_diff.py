"""git_diff 禁止候选输入任意命令。"""

from __future__ import annotations

import pytest
from execution_broker.git_diff import parse_git_diff_input


def test_parse_rejects_freeform_command() -> None:
    with pytest.raises(ValueError, match="command|禁止"):
        parse_git_diff_input(b'{"command":"git push --force"}')
    with pytest.raises(ValueError, match="command|禁止"):
        parse_git_diff_input(
            b'{"tool_ref":"git_diff","parameters":{"command":"rm -rf /"}}'
        )
    assert parse_git_diff_input(b"{}") is None
    assert parse_git_diff_input(b'{"parameters":{}}') is None
