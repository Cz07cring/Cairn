"""GoalWorkflow 启动载荷：可选键是**白名单**透传，新增旋钮必须登记，否则静默丢弃。

**缺陷背景**（本批发现并修复）：`activation_budget_seconds` 实现了、也在工作流里接线了，
但**不在 `client.py` 的透传白名单里** —— 调用方传了也被静默丢弃，工作流只能用 360s 默认值。
表现为「功能已实现但不可达」，属本仓反复出现的缺陷类型，且**只有读代码才看得出**。
故把载荷构造提为纯函数，用单测钉住白名单：以后新增旋钮忘记登记会红，而不是静默失效。
"""

from __future__ import annotations

from orchestration.client import build_goal_workflow_payload

_BASE = {"command_id": "cmd-1", "goal_id": "goal-1"}


def test_activation_budget_env_fallback() -> None:
    """env 兜底：运维可不改代码调整预算。"""
    payload = build_goal_workflow_payload(
        dict(_BASE), {"RING_GOAL_WORKFLOW_ACTIVATION_BUDGET_SECONDS": "900"}
    )
    assert payload["activation_budget_seconds"] == 900


def test_kwargs_win_over_env() -> None:
    """显式 kwargs 优先于 env（与既有 observe_max_ticks 同序）。"""
    payload = build_goal_workflow_payload(
        {**_BASE, "activation_budget_seconds": 1000},
        {"RING_GOAL_WORKFLOW_ACTIVATION_BUDGET_SECONDS": "900"},
    )
    assert payload["activation_budget_seconds"] == 1000


def test_whitelist_covers_every_documented_payload_knob() -> None:
    """伞形防线：工作流读的每个 payload 旋钮都必须在白名单里。

    新增旋钮若只改工作流、忘了登记白名单，本用例即红 —— 把「不可达」这类
    只能靠人工审阅发现的缺陷变成机械门禁。
    """
    knobs = (
        "observe_max_ticks",
        "enable_continue_as_new",
        "continue_as_new_on_observe_timeout",
        "command_ids_consumed",
        "skip_admit_activity_ids",
        "admitted_attempt_ids",
        "generation",
        "prior_run_id",
        "checkpoint_schema_version",
        "recovery_attempts",
        "max_recovery_attempts",
        "intent_valid_until",
        "intent_ttl_seconds",
        "recovery_enabled",
        "activation_budget_seconds",
        "goal_review_interval_seconds",
        "max_goal_reviews",
        "goal_reviews_requested",
        # 跨活动推进（advance-on-ready）：漏登记 = 调用方传了也被静默丢弃
        "advance_hops",
        "max_advance_hops",
        "enable_advance",
        "advanced_activity_ids",
    )
    kwargs = {**_BASE}
    # 用可区分的哨兵值，避免 falsy 值被 None 语义吞掉
    sentinels = {
        "observe_max_ticks": 7,
        "enable_continue_as_new": True,
        "continue_as_new_on_observe_timeout": True,
        "command_ids_consumed": ["c1"],
        "skip_admit_activity_ids": ["a1"],
        "admitted_attempt_ids": ["t1"],
        "generation": 2,
        "prior_run_id": "run-1",
        "checkpoint_schema_version": 1,
        "recovery_attempts": 1,
        "max_recovery_attempts": 3,
        "intent_valid_until": "2030-01-01T00:00:00+00:00",
        "intent_ttl_seconds": 60,
        "recovery_enabled": True,
        "activation_budget_seconds": 600,
        "goal_review_interval_seconds": 120,
        "max_goal_reviews": 4,
        "goal_reviews_requested": 1,
        # 跨活动推进（advance-on-ready-v1）
        "advance_hops": 3,
        "max_advance_hops": 50,
        "enable_advance": True,
        "advanced_activity_ids": ["act-1"],
    }
    kwargs.update(sentinels)
    payload = build_goal_workflow_payload(kwargs, {})
    missing = [k for k in knobs if payload.get(k) != sentinels[k]]
    assert not missing, f"以下旋钮未进入载荷白名单（会被静默丢弃）: {missing}"
