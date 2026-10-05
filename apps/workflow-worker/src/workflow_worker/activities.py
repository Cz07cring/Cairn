"""Temporal Activity 实现（允许 IO）；与 Workflow 模块分离。

实现位于 orchestration.kernel_activities；本包再导出供 Worker 注册。
控制 Activities：ensure / list / admit（PLAN、EXECUTE、及 AUDIT/INTEGRATE/FINALIZE 通用）/
普通观察 / CAN 水位归属观察；不写 Goal DONE、不调模型/工具。

**新增 Activity 必须同时登记到 KERNEL_ACTIVITIES**：Worker 注册面是显式清单，
漏登记时工作流会以「Activity function X is not registered on this worker」失败 ——
2026-09-15 实测：新增 admit_runtime_action 后未登记，工作流在
`EXECUTE:SUCCEEDED → AUDIT:READY` 处直接 FAILED（本仓 AGENTS §17 同族坑：
有实现无写入方/无注册方 = 功能不可达）。
"""

from __future__ import annotations

from orchestration.kernel_activities import (
    admit_execute_action,
    admit_plan_action,
    admit_runtime_action,
    configure_kernel_activity_ports,
    ensure_goal_delivery,
    list_runtime_actions,
    observe_activity_status,
    observe_carried_activation_activity_status,
    observe_carried_plan_activity_status,
    ping_kernel,
    read_activity_status,
)

# Worker 注册面
KERNEL_ACTIVITIES = [
    ensure_goal_delivery,
    list_runtime_actions,
    admit_plan_action,
    admit_execute_action,
    admit_runtime_action,
    observe_activity_status,
    observe_carried_plan_activity_status,
    observe_carried_activation_activity_status,
    ping_kernel,
]

__all__ = [
    "KERNEL_ACTIVITIES",
    "admit_execute_action",
    "admit_plan_action",
    "admit_runtime_action",
    "configure_kernel_activity_ports",
    "ensure_goal_delivery",
    "list_runtime_actions",
    "observe_activity_status",
    "observe_carried_activation_activity_status",
    "observe_carried_plan_activity_status",
    "ping_kernel",
    "read_activity_status",
]
