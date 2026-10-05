"""恢复意图窗口（intent TTL）：由权威剩余预算驱动。

预算读数的正确性（起点、单调、失败关闭、溢出）已移入
`tests/unit/test_budget_clock.py` —— 本文件只测**窗口选择**的优先级。

优先级（裁定 §门3 + Codex 复核）：
- 有权威剩余 → `min(剩余, 上限)`；**显式 TTL 只能收紧、不能放大**
- 无权威读数（老历史）→ 显式 TTL，否则过渡默认（逐字保留旧行为）
"""

from __future__ import annotations

from orchestration.temporal_workflows import (
    _DEFAULT_INTENT_TTL_SECONDS,
    _INTENT_TTL_CAP_SECONDS,
    _resolve_intent_ttl_seconds,
)

_MAX_SAFE = 9007199254740991


def test_remaining_used_when_shorter_than_cap() -> None:
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=None, budget_remaining_seconds=1800
        )
        == 1800
    )


def test_remaining_capped_when_longer_than_cap() -> None:
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=None, budget_remaining_seconds=5 * 24 * 3600
        )
        == _INTENT_TTL_CAP_SECONDS
    )


def test_max_safe_budget_is_capped_not_overflowed() -> None:
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=None, budget_remaining_seconds=_MAX_SAFE
        )
        == _INTENT_TTL_CAP_SECONDS
    )


def test_explicit_ttl_can_tighten_but_not_expand_budget() -> None:
    """Codex P1-3：显式 TTL 只能收紧权威剩余，不得放大。"""
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=60, budget_remaining_seconds=300
        )
        == 60
    )
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=_MAX_SAFE, budget_remaining_seconds=300
        )
        == 300
    )


def test_no_budget_reading_falls_back_to_old_behaviour() -> None:
    """老历史（无预算读数）：逐字保留旧行为 —— 重放安全的依据。"""
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=77, budget_remaining_seconds=None
        )
        == 77
    )
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=None, budget_remaining_seconds=None
        )
        == _DEFAULT_INTENT_TTL_SECONDS
    )


def test_zero_remaining_yields_one_but_caller_must_have_aborted() -> None:
    """remaining=0 正常路径不会走到（workflow 已先行失败关闭）。

    钉住函数契约：即便被误调也不返回 0/负数；"不可恢复"语义由 `budget_exhausted`
    与 `budget_status` 承担，不由 1 秒窗口暗示。
    """
    assert (
        _resolve_intent_ttl_seconds(
            explicit_ttl_seconds=None, budget_remaining_seconds=0
        )
        == 1
    )
