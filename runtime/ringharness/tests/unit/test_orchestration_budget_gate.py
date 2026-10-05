"""编排侧预算**只读第二道门**：`_interpret_wall_budget` 与 `_as_nonnegative_int`。

背景：预算的**写入方**是 Kernel（`storage/budget_clock`，Cursor 批次）；编排层只读，
用于在发起 admit 前多一道门（Codex 复核 P1-3 的"第二道门"）。
本文件只测纯函数，不连库。

关键语义：
- 读数不可解析 → **失败关闭**（`(None, True)`），不得当作"消耗 0"（那是失败开放）；
- `elapsed` 字段**缺失**是合法的（Kernel 尚未计量），按 0 处理；
- 全程整数减法，合同极大 `wall_clock` 不溢出。
"""

from __future__ import annotations

from orchestration.kernel_activities import _as_nonnegative_int, _interpret_wall_budget

_MAX_SAFE = 9007199254740991


def test_remaining_computed_from_persisted_elapsed() -> None:
    assert _interpret_wall_budget(3600, 600) == (3000, False)


def test_exhausted_at_and_beyond_limit() -> None:
    assert _interpret_wall_budget(3600, 3600) == (0, True)
    assert _interpret_wall_budget(3600, 9999) == (0, True)


def test_missing_elapsed_is_unmetered_not_corrupt() -> None:
    """字段缺失＝Kernel 尚未计量（合法），按 0 消耗 —— 不是失败。"""
    assert _interpret_wall_budget(3600, None) == (3600, False)


def test_corrupt_elapsed_fails_closed() -> None:
    """读数损坏 → 失败关闭，**不得**解释为消耗 0（否则白送足额窗口）。"""
    for bad in ("abc", -1, {}, []):
        assert _interpret_wall_budget(3600, bad) == (None, True), bad


def test_corrupt_wall_clock_fails_closed() -> None:
    """合同预算不可读：既不假定"无限制"，也不假定"已耗尽"。"""
    for bad in (None, "", "abc", 0, -1):
        assert _interpret_wall_budget(bad, 0) == (None, True), bad


def test_max_safe_wall_clock_does_not_overflow() -> None:
    """合同 wall_clock 无上界（可达 9007199254740991）；全程整数故不溢出。"""
    assert _interpret_wall_budget(_MAX_SAFE, 0) == (_MAX_SAFE, False)


def test_jsonb_text_forms_accepted() -> None:
    """jsonb `->>` 取出为文本，须能解析。"""
    assert _interpret_wall_budget("3600", "600") == (3000, False)


def test_bool_is_rejected_as_readings() -> None:
    """Python `bool` 是 int 子类；预算读数不接受它（防 True/False 被当 1/0）。"""
    assert _as_nonnegative_int(True) is None
    assert _as_nonnegative_int(False) is None
