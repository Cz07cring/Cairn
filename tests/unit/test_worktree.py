"""Attempt 工作区配置缺失时的失败关闭边界。"""

from uuid import uuid4

import pytest
from execution_broker.worktree import (
    WorkspaceUnavailable,
    ensure_attempt_workspace,
)


def test_required_without_env_fails(monkeypatch):
    monkeypatch.delenv("RING_WORKSPACE_ROOT", raising=False)
    with pytest.raises(WorkspaceUnavailable):
        ensure_attempt_workspace(uuid4(), uuid4(), required=True)
