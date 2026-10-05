"""Plan / Task 合同与资源（05§3.1）；发布后才有 plan_revision。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from .audits import AuditCandidateOutcome, AuditGoalReviewOutcome
from .candidates import ExecuteSuccessOutcome
from .effects import ReconcileSuccessOutcome
from .finalization import FinalizeSuccessOutcome
from .goals import Budget, Criterion, Digest, NonnegativeInt, PositiveInt, Text
from .integrate import IntegrateSuccessOutcome
from .memory import IndexMemorySuccessOutcome
from .models import ProbeSuccessOutcome
from .projects import Contract
from .runtime import LeaseIdentity, Resources
from .skills import ValidateSkillSuccessOutcome

TaskStatus = Literal[
    "PENDING",
    "READY",
    "RUNNING",
    "VERIFYING",
    "RETRYING",
    "BLOCKED",
    "STALE",
    "RECOVERING",
    "DONE",
    "FAILED",
    "CANCELLED",
]
PlanStatus = Literal["CANDIDATE", "PUBLISHED", "REJECTED"]
Risk = Literal["low", "medium", "high"]


class TaskRetryPolicy(Contract):
    max_execution_rounds: PositiveInt
    max_audit_attempts_per_candidate: PositiveInt
    max_activity_retries: NonnegativeInt


class Deliverable(Contract):
    kind: Text
    required: StrictBool


class TaskContract(Contract):
    objective: str = Field(min_length=1, max_length=10000)
    depends_on: list[UUID] = Field(default_factory=list, max_length=10000)
    input_artifact_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    deliverables: list[Deliverable] = Field(max_length=10000)
    acceptance: list[Criterion] = Field(min_length=1, max_length=200)
    covers_goal_criterion_ids: list[Text] = Field(max_length=10000)
    allowed_paths: list[Text] = Field(max_length=10000)
    protected_paths: list[Text] = Field(max_length=10000)
    required_capabilities: list[Text] = Field(max_length=10000)
    budget: Budget
    retry_policy: TaskRetryPolicy
    resources: Resources
    risk: Risk

    @model_validator(mode="after")
    def validate_task(self):
        if not any(c.required for c in self.acceptance):
            raise ValueError("at least one required acceptance")
        if len({c.id for c in self.acceptance}) != len(self.acceptance):
            raise ValueError("duplicate acceptance id")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate depends_on")
        if len(set(self.covers_goal_criterion_ids)) != len(self.covers_goal_criterion_ids):
            raise ValueError("duplicate covers_goal_criterion_ids")
        return self


class PlanTaskNode(Contract):
    id: UUID
    contract: TaskContract
    replaces_task_id: UUID | None = None


class CoverageEntry(Contract):
    goal_criterion_id: Text
    task_id: UUID
    task_acceptance_id: Text
    verification_profile_id: UUID


class PlanCreate(Contract):
    expected_plan_revision: PositiveInt | None
    reason: str = Field(min_length=1, max_length=10000)
    tasks: list[PlanTaskNode] = Field(min_length=1, max_length=1000)
    coverage: list[CoverageEntry] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def validate_plan(self):
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task id")
        known = set(ids)
        for task in self.tasks:
            if task.id in task.contract.depends_on:
                raise ValueError("task depends on itself")
            for dep in task.contract.depends_on:
                if dep not in known:
                    raise ValueError("depends_on missing task")
        for entry in self.coverage:
            if entry.task_id not in known:
                raise ValueError("coverage task missing")
        return self


class PlanResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    goal_id: UUID
    plan_revision: PositiveInt | None
    status: PlanStatus
    reason: Text
    tasks: list[PlanTaskNode]
    coverage: list[CoverageEntry]
    content_digest: Digest | None = None
    source_plan_input_id: UUID | None = None
    source_plan_input_digest: Digest | None = None


class TaskResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    goal_id: UUID
    contract: TaskContract
    contract_revision: PositiveInt
    plan_revision: PositiveInt
    state_revision: PositiveInt
    status: TaskStatus
    block_reason: str | None = None
    resume_state: TaskStatus | None = None
    work_lineage_id: UUID
    execution_round: PositiveInt
    latest_checkpoint_id: UUID | None = None
    replaces_task_id: UUID | None = None
    contract_digest: Digest


class PlanSuccessOutcome(Contract):
    plan: PlanCreate


class ActivityOutcomeRequest(Contract):
    lease: LeaseIdentity
    expected_state_revision: PositiveInt
    outcome: (
        PlanSuccessOutcome
        | ProbeSuccessOutcome
        | ExecuteSuccessOutcome
        | AuditCandidateOutcome
        | AuditGoalReviewOutcome
        | IntegrateSuccessOutcome
        | FinalizeSuccessOutcome
        | ReconcileSuccessOutcome
        | ValidateSkillSuccessOutcome
        | IndexMemorySuccessOutcome
    )
