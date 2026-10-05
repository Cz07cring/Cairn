"""RunActivation 派发预算：可放大、只许放大、且不得超出观察窗口。

**缺陷背景**（本批修复的缺口）：`RunActivation` 的 `start_to_close_timeout` 原为硬编码
**360 秒**。而 EXECUTE 阶段的官方 AgentLoop 要做**多轮**模型往返
（读→测→改→复测→封存），Runner 单侧**单次**模型调用的 idle 超时默认就有 **180 秒**。
即 360s 只够约两轮 —— 一个健康、正在推进的自主多步循环会被 Activity 超时打断，
且打断后走「转入 Kernel 状态核对」而非明确报错，属**静默降级**。

本批把预算改为可经 payload 放大（与 `observe_max_ticks` 同一模式：payload 配置、
缺省不变、patch 门保护旧历史），并钉住两条约束：

1. **只许放大**：低于地板（= 钉扎 360s）的值不被采用；
2. **不得超出观察窗口**：否则活动仍合法运行时工作流已判 `OBSERVE_TIMEOUT`
   → CAN → 上限 → `RECOVERY_ABANDONED`，把健康长任务当作不可恢复放弃
   —— 与本仓已修的「29 秒观察窗口」同族。

**为什么工作流内用不抛错的 `_effective_activation_budget_seconds`（本批实测）**：
在工作流代码里 `raise` 会让 Workflow Task 持续失败并被 Temporal 反复重试，
表现为**工作流永久挂住** —— 既不完成也不报错，比「用错预算」更糟。
故冲突以「安全退回钉扎值 + 回报原因」表达，原因会进工作流返回值
（`activation_budget_notes`），使误配**可见**而非静默。
"""

from __future__ import annotations

import pytest
from orchestration.temporal_workflows import (
    _ACTIVATION_BUDGET_FLOOR_SECONDS,
    _DEFAULT_ACTIVATION_BUDGET_SECONDS,
    _DEFAULT_OBSERVE_MAX_TICKS,
    _effective_activation_budget_seconds,
    _observe_window_seconds,
    _resolve_activation_budget_seconds,
)


# --------------------------------------------------- 解析（启动/API 边界用，会抛）
def test_shrinking_below_floor_raises_at_boundary() -> None:
    """边界（API/启动）处：低于地板抛错，不静默取默认。"""
    for bad in (0, 1, 100, _ACTIVATION_BUDGET_FLOOR_SECONDS - 1):
        with pytest.raises(ValueError, match="低于地板"):
            _resolve_activation_budget_seconds(bad)


def test_non_integer_raises_at_boundary() -> None:
    for bad in ("abc", "1800s", [1800], {"seconds": 1800}):
        with pytest.raises(ValueError, match="须为整数"):
            _resolve_activation_budget_seconds(bad)


# ------------------------------------------- 工作流内生效值（不抛错，安全退回）
def test_effective_rejects_budget_beyond_window() -> None:
    """**关键**：预算超出窗口时不被采用，并回报原因（避免新的静默误杀）。"""
    budget, note = _effective_activation_budget_seconds(
        3600, observe_max_ticks=_DEFAULT_OBSERVE_MAX_TICKS
    )
    assert budget == _DEFAULT_ACTIVATION_BUDGET_SECONDS
    assert note is not None and "budget_exceeds_observe_window" in note


def test_effective_rejects_shrinking_and_garbage_without_raising() -> None:
    """工作流内**绝不抛错**（抛错会挂住工作流），一律安全退回并回报。"""
    for bad in (60, 0, -1, "abc", "1800s"):
        budget, note = _effective_activation_budget_seconds(
            bad, observe_max_ticks=_DEFAULT_OBSERVE_MAX_TICKS
        )
        assert budget == _DEFAULT_ACTIVATION_BUDGET_SECONDS
        assert note is not None and "explicit_budget_rejected" in note


def test_effective_never_exceeds_window() -> None:
    """穷举常见配置：生效值恒不超过窗口（结构上杜绝「活动仍跑、工作流已超时」）。"""
    for ticks in (1, 2, 5, 30, 60, 90, 200):
        window = _observe_window_seconds(ticks)
        for raw in (None, 360, 1800, 3600, 86400, "abc", 60):
            budget, _note = _effective_activation_budget_seconds(
                raw, observe_max_ticks=ticks
            )
            assert budget <= max(window, _DEFAULT_ACTIVATION_BUDGET_SECONDS)
