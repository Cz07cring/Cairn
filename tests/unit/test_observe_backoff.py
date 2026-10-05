"""观察轮询退避：默认观察窗口必须覆盖一次 `RunActivation` 的完整超时。

**缺陷背景**（本批修复）：观察循环原为固定 `sleep(1s)` × `observe_max_ticks`（默认 30），
合计**约 29 秒**。而 `RunActivation` 的 `start_to_close_timeout` 为 **360 秒**
（本机 Qwen 超时亦有 180 秒），故一个**健康但耗时**的 harness 运行会被判 `OBSERVE_TIMEOUT`
→ Continue-As-New → 次数上限（默认 3）→ `RECOVERY_ABANDONED`，把正常长任务当作
"不可恢复"放弃。

**不变量**：默认轮次下的等待总时长必须 ≥ 一次 `RunActivation` 的超时（360s），
否则"观察窗口"在语义上无法容纳它正在观察的对象。
"""

from __future__ import annotations

from orchestration.temporal_workflows import (
    _DEFAULT_OBSERVE_MAX_TICKS,
    _OBSERVE_BACKOFF_CAP_SECONDS,
    _observe_poll_delay_seconds,
)

# 一次 RunActivation 的 start_to_close 超时（见 AGENTS：GoalWorkflow 的 RunActivation 360s）
_RUN_ACTIVATION_TIMEOUT_SECONDS = 360


def _total_wait_seconds(ticks: int) -> int:
    """N 轮观察的等待总时长（末轮不 sleep，故为 0..N-2）。"""
    return sum(_observe_poll_delay_seconds(t) for t in range(max(0, ticks - 1)))


def test_cap_is_respected_for_large_tick() -> None:
    assert _observe_poll_delay_seconds(10_000) == _OBSERVE_BACKOFF_CAP_SECONDS


def test_negative_tick_is_defensive_not_crashing() -> None:
    assert _observe_poll_delay_seconds(-1) == 1


def test_default_window_covers_one_run_activation_timeout() -> None:
    """**核心不变量**：默认观察窗口须 ≥ 一次 RunActivation 超时，否则长度不足以观察其对象。"""
    total = _total_wait_seconds(_DEFAULT_OBSERVE_MAX_TICKS)
    assert total >= _RUN_ACTIVATION_TIMEOUT_SECONDS, (
        f"观察窗口 {total}s 短于 RunActivation 超时 {_RUN_ACTIVATION_TIMEOUT_SECONDS}s"
        "—— 健康的长任务会被误判 OBSERVE_TIMEOUT"
    )


def test_single_tick_has_no_wait() -> None:
    """`observe_max_ticks=1`（测试常用）不应产生任何等待。"""
    assert _total_wait_seconds(1) == 0
