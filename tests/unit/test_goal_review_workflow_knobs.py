"""GoalReview → Workflow 启动旋钮：合同派生，禁止「有实现无写入方」。"""

from control_kernel.storage.audits import goal_review_workflow_start_knobs


def test_goal_review_workflow_start_knobs_small_wall_floor():
    """小墙钟仍给出 ≥30s 间隔（对齐 Workflow 下限，避免「有旋钮零触发」）。"""
    knobs = goal_review_workflow_start_knobs(
        {
            "retry_policy": {"max_plan_revisions": 2},
            "budget": {"wall_clock_seconds": 10},
        }
    )
    assert knobs["max_goal_reviews"] == 2
    assert knobs["goal_review_interval_seconds"] >= 30
