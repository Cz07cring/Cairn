"""PlanningFeedback 协议（doc/05 D03）。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .projects import Contract
from .skills import CriterionResult
from .verification import Digest


class PlanningFeedbackResource(Contract):
    id: UUID
    created_at: datetime
    goal_id: UUID
    task_id: UUID | None
    candidate_manifest_id: UUID
    goal_contract_revision: int = Field(ge=1)
    task_contract_revision: int | None = Field(default=None, ge=1)
    plan_revision: int = Field(ge=1)
    aggregation_digest: Digest
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    public_criterion_results: list[CriterionResult] = Field(max_length=10000)
    blocking_reason_codes: list[str] = Field(default_factory=list, max_length=10000)
    content_digest: Digest
