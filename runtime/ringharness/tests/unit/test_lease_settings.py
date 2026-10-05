"""RING_LEASE_TTL_SECONDS / RING_HEARTBEAT_SECONDS 配置往返与 doc/08 不变量。

证明 Settings 真读环境变量（非仅有默认值）；≠ Goal DONE。
"""

from __future__ import annotations

import pytest
from control_api.settings import Settings
from pydantic import ValidationError


def test_env_lease_ttl_and_heartbeat_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RING_LEASE_TTL_SECONDS", "180")
    monkeypatch.setenv("RING_HEARTBEAT_SECONDS", "20")
    cfg = Settings()
    assert cfg.lease_ttl_seconds == 180
    assert cfg.heartbeat_seconds == 20


def test_ttl_below_three_heartbeats_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RING_LEASE_TTL_SECONDS", "40")
    monkeypatch.setenv("RING_HEARTBEAT_SECONDS", "20")
    with pytest.raises(ValidationError, match="3×RING_HEARTBEAT_SECONDS"):
        Settings()
