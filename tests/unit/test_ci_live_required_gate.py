"""RING_CI_LIVE_REQUIRED：缺环境时 fail 而非 skip。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from live_chat_gate import live_required, skip_or_fail_live


def test_ci_live_required_fails_instead_of_skip(monkeypatch):
    monkeypatch.setenv("RING_CI_LIVE_REQUIRED", "1")
    assert live_required() is True
    with pytest.raises(pytest.fail.Exception, match="RING_CI_LIVE_REQUIRED"):
        skip_or_fail_live("缺 harness")


def test_ci_live_optional_still_skips(monkeypatch):
    monkeypatch.delenv("RING_CI_LIVE_REQUIRED", raising=False)
    assert live_required() is False
    with pytest.raises(pytest.skip.Exception, match="缺 harness"):
        skip_or_fail_live("缺 harness")
