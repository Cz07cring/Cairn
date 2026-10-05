"""AUDIT 只读聚合协议（GoalAuditItem / AuditAggregation）。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .goals import Digest, Epoch, NonnegativeInt, PositiveInt, Text
from .projects import Contract
from .skills import CriterionResult


class AuditCreate(Contract):
    """worker 提交的审计业务内容；producer/attempt 由 Kernel 从 lease 填入。"""

    subject_candidate_manifest_id: UUID
    goal_contract_revision: PositiveInt
    task_contract_revision: PositiveInt | None = None
    verification_profile_id: UUID
    layer: Literal["MECHANICAL", "SEMANTIC", "ADVERSARIAL", "GLOBAL"]
    audit_round: int = Field(ge=1)
    verifier_run_ids: list[UUID] = Field(min_length=1, max_length=10000)
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    criterion_results: list[CriterionResult] = Field(min_length=1, max_length=10000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    reason: Text


class AuditCandidateOutcome(Contract):
    target_type: Literal["CANDIDATE"] = "CANDIDATE"
    audit: AuditCreate


class AuditResource(Contract):
    id: UUID
    created_at: datetime
    project_id: UUID
    goal_id: UUID
    task_id: UUID | None
    producer_activity_id: UUID
    producer_attempt_id: UUID
    subject_candidate_manifest_id: UUID
    goal_contract_revision: PositiveInt
    task_contract_revision: PositiveInt | None
    verification_profile_id: UUID
    layer: Text
    audit_round: int
    verifier_run_ids: list[UUID]
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    criterion_results: list[CriterionResult]
    evidence_ids: list[UUID]
    reason: Text
    content_digest: Digest


class MissingAuditItem(Contract):
    verification_profile_id: UUID
    layer: Literal["MECHANICAL", "SEMANTIC", "ADVERSARIAL", "GLOBAL"]
    criterion_id: Text


class AuditAggregation(Contract):
    """Kernel 对候选验收槽的权威汇总；不引入第四种 verdict。"""

    subject_candidate_manifest_id: UUID
    goal_contract_revision: PositiveInt
    task_contract_revision: PositiveInt | None
    plan_revision: PositiveInt
    required_set_digest: Digest
    accepted_audit_ids: list[UUID] = Field(default_factory=list)
    accepted_assessment_ids: list[UUID] = Field(default_factory=list)
    pending_verification_count: int = Field(default=0, ge=0)
    trust_revision: Epoch
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"]
    blocking_reason_codes: list[Text] = Field(default_factory=list)
    missing_items: list[MissingAuditItem] = Field(default_factory=list)


class GoalReviewEnsureRequest(Contract):
    """Kernel 去重创建 AUDIT(target=GOAL_REVIEW)；快照由服务端钉扎。"""

    trigger_key: Text = Field(min_length=1, max_length=512)
    """可选复盘序号；与 trigger_key 一并写入 assignments，供编排对账。"""
    review_seq: PositiveInt | None = None


class GoalReviewEnsureResult(Contract):
    activity_id: UUID
    review_snapshot_digest: Digest
    created: bool
    """剩余可新建 GOAL_REVIEW 次数（按 max_plan_revisions）；去重命中不扣减。"""
    reviews_remaining: NonnegativeInt | None = None
    """合同推导的最大复盘次数（= max_plan_revisions）。"""
    max_reviews: PositiveInt | None = None
    """两次新建 GOAL_REVIEW 的最小间隔（秒）；编排侧可作 timer 下界。"""
    min_interval_seconds: NonnegativeInt | None = None
    """工程进展停滞阈值（秒）；未停滞时 Kernel 会拒绝新建。"""
    stagnation_seconds: NonnegativeInt | None = None
    marks_goal_done: Literal[False] = False


class GoalReviewBudgetSnapshot(Contract):
    """GoalReview 复盘预算只读快照；≠ Goal DONE。

    eligible_now 仅表示当前是否通过预算/间隔/停滞门（不保证 claim 成功）。
    blocking_reason_code 在不可新建时给出 GOAL_REVIEW_* 码。
    """

    max_reviews: PositiveInt
    reviews_used: NonnegativeInt
    reviews_remaining: NonnegativeInt
    min_interval_seconds: NonnegativeInt
    stagnation_seconds: NonnegativeInt
    seconds_since_last_review: NonnegativeInt | None = None
    seconds_since_engineering_progress: NonnegativeInt | None = None
    eligible_now: bool
    blocking_reason_code: Text | None = None
    marks_goal_done: Literal[False] = False


class GoalReviewFinding(Contract):
    code: Text
    severity: Literal["INFO", "WARN", "BLOCKER"]
    evidence_ids: list[UUID] = Field(default_factory=list)
    recommendation: Text


class GoalReviewCreate(Contract):
    """worker 提交的周期诊断内容；producer/attempt 由 Kernel 从 lease 填入。"""

    goal_contract_revision: PositiveInt
    plan_revision: PositiveInt | None = None
    review_snapshot_digest: Digest
    findings: list[GoalReviewFinding] = Field(default_factory=list, max_length=10000)


class AuditGoalReviewOutcome(Contract):
    target_type: Literal["GOAL_REVIEW"] = "GOAL_REVIEW"
    review: GoalReviewCreate


class GoalReviewResource(Contract):
    id: UUID
    created_at: datetime
    project_id: UUID
    goal_id: UUID
    producer_activity_id: UUID
    producer_attempt_id: UUID
    goal_contract_revision: PositiveInt
    plan_revision: PositiveInt | None
    review_snapshot_digest: Digest
    findings: list[GoalReviewFinding] = Field(default_factory=list)
    content_digest: Digest


class CandidateAuditItem(Contract):
    record_type: Literal["CANDIDATE_AUDIT"] = "CANDIDATE_AUDIT"
    audit: AuditResource
    aggregation: AuditAggregation


class GoalReviewItem(Contract):
    record_type: Literal["GOAL_REVIEW"] = "GOAL_REVIEW"
    review: GoalReviewResource


GoalAuditItem = Annotated[
    CandidateAuditItem | GoalReviewItem,
    Field(discriminator="record_type"),
]
