"""Goal 合同与资源类型；持久化前须事务内核对配置引用。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from .projects import Contract

NonnegativeInt = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
DecimalString = Annotated[
    str, Field(max_length=32, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{0,5}[1-9])?$")
]
Text = Annotated[str, Field(max_length=10000)]
Epoch = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=32)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
GoalStatus = Literal[
    "DRAFT",
    "PLANNING",
    "RUNNING",
    "VERIFYING",
    "PAUSING",
    "PAUSED",
    "CANCELLING",
    "CANCELLED",
    "BLOCKED",
    "FAILED",
    "DONE",
]


class Budget(Contract):
    wall_clock_seconds: PositiveInt
    max_tokens: NonnegativeInt
    max_cost_usd: DecimalString
    max_tool_calls: NonnegativeInt
    max_network_calls: NonnegativeInt
    max_disk_bytes: NonnegativeInt
    max_gpu_seconds: DecimalString | None


class RetryPolicy(Contract):
    max_execution_rounds: PositiveInt = 4
    max_audit_attempts_per_candidate: PositiveInt = 3
    max_activity_retries: NonnegativeInt = 3
    max_plan_revisions: PositiveInt = 10


class Criterion(Contract):
    id: str = Field(min_length=1, max_length=10000)
    description: str = Field(min_length=1, max_length=10000)
    required: StrictBool
    verification_profile_id: UUID


class GoalCreate(Contract):
    project_id: UUID
    objective: str = Field(min_length=1, max_length=10000)
    success_criteria: list[Criterion] = Field(min_length=1, max_length=200)
    constraints: list[Text] = Field(max_length=10000)
    budget: Budget
    retry_policy: RetryPolicy
    policy_id: UUID
    model_profile_id: UUID
    skill_set_id: UUID
    base_commit: Text

    @model_validator(mode="after")
    def validate_criteria(self):
        if not any(c.required for c in self.success_criteria):
            raise ValueError("at least one required criterion")
        if len({c.id for c in self.success_criteria}) != len(self.success_criteria):
            raise ValueError("duplicate criterion id")
        if len(set(self.constraints)) != len(self.constraints):
            raise ValueError("duplicate constraint")
        return self


class GoalContractUpdate(Contract):
    """仅 DRAFT/PAUSED 可改合同；project_id 不可变更。"""

    expected_state_revision: PositiveInt
    contract: GoalCreate
    reason: str = Field(min_length=1, max_length=2000)


class PlanInputModeSelection(Contract):
    """操作员在 Goal.start 前选择规划输入准入模式。"""

    expected_state_revision: PositiveInt
    mode: Literal["OPTIONAL", "REQUIRED"]


class CriterionSummary(Contract):
    verified: NonnegativeInt
    total: NonnegativeInt


class BarrierResource(Contract):
    id: UUID
    goal_id: UUID
    write_epoch: Epoch
    status: Literal["DRAINING", "SEALED", "RELEASED", "ABORTED"]
    contract_revision: PositiveInt
    plan_revision: PositiveInt
    candidate_manifest_id: UUID | None = None
    in_flight_engineering: int = Field(ge=0)
    unknown_effects: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class BudgetUsage(Contract):
    consumed_tokens: NonnegativeInt
    reserved_tokens: NonnegativeInt
    consumed_cost_usd: DecimalString
    reserved_cost_usd: DecimalString
    cost_status: Literal["CONFIRMED", "ESTIMATED", "UNKNOWN"]
    elapsed_wall_seconds: NonnegativeInt
    active_seconds: NonnegativeInt
    tool_calls: NonnegativeInt
    network_calls: NonnegativeInt
    disk_bytes: NonnegativeInt
    gpu_seconds: DecimalString | None
    observed_at: datetime


class GoalResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    status: GoalStatus
    state_revision: PositiveInt
    contract_revision: PositiveInt
    plan_revision: PositiveInt | None
    plan_input_mode: Literal["OPTIONAL", "REQUIRED"] = "OPTIONAL"
    contract: GoalCreate
    contract_digest: Digest
    previous_status: GoalStatus | None
    integration_commit: str | None = Field(default=None, max_length=10000)
    criterion_summary: CriterionSummary
    budget_usage: BudgetUsage
    block_reason: str | None = Field(default=None, max_length=2000)
    write_epoch: Epoch
    # 默认 LEGACY：存量 Goal / 幂等缓存缺字段时不误切 TEMPORAL
    orchestration_backend: Literal["LEGACY", "TEMPORAL"] = "LEGACY"
    owner_epoch: Epoch = "1"
    barrier: BarrierResource | None = None
    release_manifest_id: UUID | None = None


class OrchestrationAbandonmentResource(Contract):
    """编排放弃事实只读投影（Issue #24）；marks_goal_done 恒为 false。"""

    id: UUID
    project_id: UUID
    goal_id: UUID
    generation: NonnegativeInt
    reason: str = Field(min_length=1, max_length=200)
    prior_run_id: str | None = Field(default=None, max_length=200)
    worker_id: UUID
    created_at: datetime
    marks_goal_done: StrictBool = False


class OrchestrationAbandonmentCreate(Contract):
    """编排层提交放弃裁决（Issue #24）；仅 ACTIVE worker；幂等 (goal_id, generation)。"""

    reason: str = Field(min_length=1, max_length=200)
    generation: NonnegativeInt
    prior_run_id: str | None = Field(default=None, max_length=200)


class GoalWallBudgetSnapshot(Contract):
    """Goal 墙钟预算只读快照（Issue #20）；推进后投影；≠ Goal DONE。

    budget_usage_unknown=true 时其余数值字段为 null，且 budget_exhausted 视为 true（失败关闭）。
    """

    budget_usage_unknown: StrictBool
    elapsed_wall_seconds: NonnegativeInt | None = None
    active_seconds: NonnegativeInt | None = None
    budget_remaining_wall_seconds: NonnegativeInt | None = None
    budget_exhausted: StrictBool
    wall_clock_limit_seconds: NonnegativeInt | None = None
    marks_goal_done: StrictBool = False
