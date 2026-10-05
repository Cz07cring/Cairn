"""seal_candidate 禁止候选自行报告文件或命令。"""

from __future__ import annotations

import pytest
from execution_broker.seal_candidate import parse_seal_candidate_input


def test_parse_rejects_self_reported_files() -> None:
    with pytest.raises(ValueError, match="files|command"):
        parse_seal_candidate_input(b'{"files":[{"path":"x","digest":"sha256:ab"}]}')
    with pytest.raises(ValueError, match="files|command"):
        parse_seal_candidate_input(b'{"command":"git commit"}')
