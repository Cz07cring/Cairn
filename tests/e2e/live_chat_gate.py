"""live e2e CI 门闩：RING_CI_LIVE_REQUIRED=1 时缺环境 fail-closed。"""

from __future__ import annotations

import os

import pytest


def live_required() -> bool:
    return (os.environ.get("RING_CI_LIVE_REQUIRED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def skip_or_fail_live(reason: str) -> None:
    if live_required():
        pytest.fail(f"RING_CI_LIVE_REQUIRED=1：{reason}")
    pytest.skip(reason)
