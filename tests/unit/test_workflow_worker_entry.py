"""workflow-worker 入口：缺 RING_TEMPORAL_TARGET 时诚实空闲；不写 Goal DONE。"""

from __future__ import annotations

import logging

import pytest
from workflow_worker.service import (
    WorkerSettings,
    load_settings,
    refuse_mark_goal_done,
    run_loop,
)


def test_refuses_mark_goal_done():
    with pytest.raises(PermissionError, match="DONE"):
        refuse_mark_goal_done(goal_id="g1", status="DONE")


def test_kernel_activities_register_admit_execute():
    """M3：Worker 须显式注册 admit_execute_action / activation CAN 观察。"""
    from workflow_worker.activities import KERNEL_ACTIVITIES

    names = {getattr(a, "__name__", None) or a.__name__ for a in KERNEL_ACTIVITIES}
    # temporalio @activity.defn 保留原函数名
    assert "admit_execute_action" in names
    assert "admit_plan_action" in names
    assert "observe_carried_activation_activity_status" in names
    assert "observe_carried_plan_activity_status" in names


def test_allows_business_database_url_unlike_broker():
    """与 Broker 相反：Worker 可为 Kernel Activities 持有业务库 URL。"""
    cfg = load_settings(
        {
            "RING_DATABASE_URL": "postgresql://ring@127.0.0.1/ring",
            "RING_TEMPORAL_TARGET": "",
        }
    )
    assert cfg.database_url == "postgresql://ring@127.0.0.1/ring"
    assert cfg.temporal_target is None


def test_once_logs_workflow_worker_idle_without_target(caplog):
    caplog.set_level(logging.INFO)
    code = run_loop(
        once=True,
        settings=WorkerSettings(temporal_target=None),
    )
    assert code == 0
    assert any("workflow-worker-idle" in record.message for record in caplog.records)
