"""ReadModel 协议：GoalSnapshot / SystemStatus（doc/05）。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .approvals import ApprovalResource
from .effects import EffectResource
from .feedback import PlanningFeedbackResource
from .goals import GoalResource, NonnegativeInt
from .plans import PlanResource, TaskResource
from .projects import Contract
from .runtime import ActivityResource, CommandOperation


class TrustState(Contract):
    project_id: UUID
    trust_revision: str = Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=32)
    status: Literal["OPEN", "BLOCKED"]
    decision_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    propagation_job_id: UUID | None = None


class ComponentStatus(Contract):
    name: str = Field(min_length=1, max_length=200)
    state: Literal["HEALTHY", "DEGRADED", "UNAVAILABLE", "UNKNOWN"]
    observed_at: datetime
    reason_code: str | None = Field(default=None, max_length=200)


class ResourcePool(Contract):
    used: NonnegativeInt
    total: NonnegativeInt


class ResourceStatus(Contract):
    model: ResourcePool
    tools: ResourcePool
    browsers: ResourcePool
    queued_activities: NonnegativeInt
    quarantined_resources: NonnegativeInt
    observed_at: datetime
    stale: bool


class SystemStatus(Contract):
    trust: TrustState
    components: list[ComponentStatus] = Field(max_length=100)
    resources: ResourceStatus
    observed_at: datetime
    stale: bool


class GoalSnapshot(Contract):
    goal: GoalResource
    plan: PlanResource | None
    tasks: list[TaskResource] = Field(max_length=1000)
    activities: list[ActivityResource] = Field(max_length=1000)
    approvals: list[ApprovalResource] = Field(default_factory=list, max_length=1000)
    effects: list[EffectResource] = Field(max_length=1000)
    commands: list[CommandOperation] = Field(max_length=1000)
    planning_feedback: list[PlanningFeedbackResource] = Field(default_factory=list, max_length=100)
    feedback_truncated: bool
    latest_seq: str = Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=19)
