"""编排包（Temporal M0/M1）。

提供 FakeTemporalClient / RealTemporalClient、Relay、确定性 GoalWorkflow M1 驱动，
Kernel 控制 Activities，以及 temporalio replay-safe GoalWorkflow。
协议模块无网络；DB 写入经 control_kernel.storage.orchestration。
Python SDK 钉扎 temporalio==1.32.0；真实 Client 仅当 RING_TEMPORAL_TARGET 可连时使用。
"""

from .client import (
    DEFAULT_TASK_QUEUE,
    RUN_ACTIVATION_ACTIVITY_NAME,
    RUNNER_TASK_QUEUE,
    FakeTemporalClient,
    RealTemporalClient,
    TemporalUnavailable,
    TemporalWorkflowClosed,
    build_temporal_client,
)
from .goal_workflow import (
    GoalWorkflowKernelPorts,
    GoalWorkflowM1TickResult,
    run_goal_workflow_m1_tick,
)
from .kernel_activities import (
    admit_execute_action,
    admit_plan_action,
    configure_kernel_activity_ports,
    ensure_goal_delivery,
    is_activity_terminal_status,
    list_runtime_actions,
    observe_activity_status,
    observe_carried_activation_activity_status,
    observe_carried_plan_activity_status,
    ping_kernel,
    read_activity_status,
    record_recovery_abandonment,
)
from .protocols import (
    DeliveryReceipt,
    OrchestrationBindingContent,
    RuntimeActionRef,
    RuntimeActionsResult,
    RuntimeAdmitRequest,
    RuntimeWaitHint,
)
from .relay import ensure_workflow

__all__ = [
    "DEFAULT_TASK_QUEUE",
    "RUNNER_TASK_QUEUE",
    "RUN_ACTIVATION_ACTIVITY_NAME",
    "DeliveryReceipt",
    "FakeTemporalClient",
    "GoalWorkflowKernelPorts",
    "GoalWorkflowM1TickResult",
    "OrchestrationBindingContent",
    "RealTemporalClient",
    "RuntimeActionRef",
    "RuntimeActionsResult",
    "RuntimeAdmitRequest",
    "RuntimeWaitHint",
    "TemporalUnavailable",
    "TemporalWorkflowClosed",
    "admit_execute_action",
    "admit_plan_action",
    "build_temporal_client",
    "configure_kernel_activity_ports",
    "ensure_goal_delivery",
    "ensure_workflow",
    "is_activity_terminal_status",
    "list_runtime_actions",
    "observe_activity_status",
    "observe_carried_activation_activity_status",
    "observe_carried_plan_activity_status",
    "ping_kernel",
    "read_activity_status",
    "record_recovery_abandonment",
    "run_goal_workflow_m1_tick",
]
