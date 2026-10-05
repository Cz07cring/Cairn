"""Activity / Command 运行时协议（05§3.2）；PLAN 无工具。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from .goals import Digest, Epoch, NonnegativeInt, PositiveInt, StrictBool, Text
from .projects import Contract

ActivityKind = Literal[
    "PLAN",
    "EXECUTE",
    "AUDIT",
    "INTEGRATE",
    "RECONCILE",
    "FINALIZE",
    "PROBE_MODEL",
    "VALIDATE_SKILL",
    "INDEX_MEMORY",
    "EXPORT_EVIDENCE",
]
ActivityStatus = Literal[
    "PENDING",
    "READY",
    "RUNNING",
    "WAITING",
    "RECOVERING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
]
TargetType = Literal[
    "GOAL_PLAN",
    "TASK_WORK",
    "CANDIDATE",
    "GOAL_REVIEW",
    "INTEGRATION",
    "EFFECT",
    "FINALIZATION",
    "MODEL_PROFILE",
    "SKILL_VERSION",
    "MEMORY_INDEX",
    "RELEASE_EXPORT",
]
AttemptStatus = Literal["ACTIVE", "COMPLETED", "FAILED", "EXPIRED", "CANCELLED"]
HeartbeatControl = Literal["CONTINUE", "CHECKPOINT", "STOP"]


class ControlRequest(Contract):
    expected_state_revision: PositiveInt
    reason: str = Field(min_length=1, max_length=2000)


class ReplanRequest(Contract):
    expected_state_revision: PositiveInt
    expected_plan_revision: PositiveInt | None = None
    reason: str = Field(min_length=1, max_length=2000)


class Resources(Contract):
    cpu_millicores: PositiveInt
    memory_bytes: PositiveInt
    disk_bytes: PositiveInt
    model_slots: NonnegativeInt
    browser_slots: NonnegativeInt
    exclusive_labels: list[Text] = Field(max_length=10000)


class ActivityTarget(Contract):
    type: TargetType
    id: UUID


class ExecutionBinding(Contract):
    goal_contract_revision: PositiveInt | None
    goal_contract_digest: Digest | None
    task_contract_revision: PositiveInt | None
    task_contract_digest: Digest | None
    plan_revision: PositiveInt | None
    subject_digest: Digest
    policy_digest: Digest
    model_profile_digest: Digest | None
    skill_set_digest: Digest | None


class ActivityResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    goal_id: UUID | None
    task_id: UUID | None
    binding: ExecutionBinding
    budget_scope_id: UUID
    kind: ActivityKind
    target: ActivityTarget
    verification_assignments: list = Field(default_factory=list)
    status: ActivityStatus
    state_revision: PositiveInt
    depends_on_activity_ids: list[UUID] = Field(default_factory=list)
    wait_reason: str | None = None
    wake_at: datetime | None = None
    wait_deadline_at: datetime | None = None
    resume_state: Literal["READY"] | None = None
    retry_count: NonnegativeInt
    current_attempt_id: UUID | None = None
    resources: Resources


class CommandResult(Contract):
    """控制命令结果；探测命令额外携带 profile/activity/证据。"""

    goal_id: UUID | None = None
    task_id: UUID | None = None
    final_status: str | None = None
    plan_revision: PositiveInt | None = None
    barrier_id: UUID | None = None
    action: Literal["REVERIFY", "REWORK"] | None = None
    write_epoch: str | None = None
    activity_id: UUID | None = None
    replacement_task_id: UUID | None = None
    work_lineage_id: UUID | None = None
    profile_id: UUID | None = None
    evidence_ids: list[UUID] | None = None
    capability_status: Literal["UNVERIFIED", "VERIFIED", "FAILED"] | None = None
    effect_id: UUID | None = None
    observed_status: Literal["SUCCEEDED", "FAILED"] | None = None
    version_id: UUID | None = None
    audit_id: UUID | None = None
    verdict: Literal["PASS", "INSUFFICIENT", "FAIL"] | None = None
    artifact_id: UUID | None = None
    trust_mode: Literal["INTERNAL_COPY", "OFFLINE_VERIFIABLE"] | None = None
    # INDEX_MEMORY 成功后回填；不捏造索引字节，仅存 outcome 给出的版本串
    index_version: Text | None = None
    record_ids: list[UUID] | None = None

    @model_serializer(mode="wrap")
    def omit_nulls(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class CommandError(Contract):
    code: str
    message: str


class CommandOperation(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID | None
    goal_id: UUID | None
    kind: Literal[
        "START",
        "PAUSE",
        "RESUME",
        "CANCEL_GOAL",
        "CANCEL_TASK",
        "RETRY_TASK",
        "REPLAN",
        "RECOVER_FINALIZATION",
        "PROBE_MODEL",
        "VALIDATE_SKILL",
        "INDEX_MEMORY",
        "RECONCILE_EFFECT",
        "EXPORT_EVIDENCE",
    ]
    status: Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"]
    request_digest: Digest
    result: CommandResult | None
    error: CommandError | None


class ClaimRequest(Contract):
    kinds: list[ActivityKind] = Field(min_length=1, max_length=10)
    capabilities: list[Text] = Field(default_factory=list, max_length=10000)


class RuntimeActionRef(Contract):
    """TEMPORAL 具名动作引用；只含引用，不含模型正文或凭据。"""

    project_id: UUID
    goal_id: UUID | None
    activity_id: UUID
    action_id: UUID
    owner_epoch: Epoch
    binding_digest: Digest


class RuntimeWaitHint(Contract):
    """无 READY 动作时的持久等待提示；不表示 Goal DONE。"""

    code: Literal["NO_READY_ACTIVITIES", "GOAL_CONTROL_DRAINING", "PLAN_INPUT_REQUIRED"]
    message: str = Field(min_length=1, max_length=500)


class RuntimeActionsResult(Contract):
    """Kernel 计算的可准入动作列表；不直接派发 Runner。"""

    actions: list[RuntimeActionRef]
    wait_hint: RuntimeWaitHint | None = None


class RuntimeAdmitRequest(Contract):
    """具名准入请求；M1 以 activity_id 为准，Idempotency-Key 走 Header。"""

    activity_id: UUID


class LeaseIdentity(Contract):
    activity_id: UUID
    attempt_id: UUID
    fencing_epoch: Epoch


class ActivationTerminationCreate(Contract):
    """Worker 登记 activation 确定性终止（M3.5）；须持有 attempt；≠ Goal DONE。"""

    lease: LeaseIdentity
    reason: str = Field(min_length=1, max_length=64)
    detail: str | None = Field(default=None, max_length=2000)
    summary_artifact_id: UUID | None = None
    closeout_artifact_id: UUID | None = None


class ActivationTerminationResource(Contract):
    """activation 终止事实；marks_goal_done 恒 false。"""

    id: UUID
    project_id: UUID
    goal_id: UUID
    activity_id: UUID
    attempt_id: UUID
    reason: str = Field(min_length=1, max_length=64)
    detail: str | None = Field(default=None, max_length=2000)
    summary_artifact_id: UUID | None = None
    closeout_artifact_id: UUID | None = None
    worker_id: UUID
    created_at: datetime
    marks_goal_done: StrictBool = False


class NoProgressReviewAckRequest(Contract):
    """Operator 确认无进展复盘（GOAL_REQUIRES_REVIEW）；≠ Goal DONE。"""

    expected_state_revision: PositiveInt
    detail: str | None = Field(default=None, max_length=2000)


class GoalNudgeBudgetResource(Contract):
    """AB06 Goal 级 Nudge 预算快照；marks_goal_done 恒 false。"""

    goal_id: UUID
    project_id: UUID
    consumed: NonnegativeInt
    max_budget: PositiveInt
    updated_at: datetime | None = None
    # consume 时是否本次扣减成功；只读 GET 省略
    accepted: StrictBool | None = None
    marks_goal_done: StrictBool = False


class GoalNudgeBudgetConsumeRequest(Contract):
    """Worker 原子消耗 1 次 Nudge 预算。"""

    max_budget: PositiveInt


class GoalBanCallKeysResource(Contract):
    """AB06 Goal 级软禁同参键列表；marks_goal_done 恒 false。"""

    goal_id: UUID
    project_id: UUID
    call_keys: list[str]
    # add 时回显本次键与是否新插入；只读 GET 省略
    call_key: str | None = None
    inserted: StrictBool | None = None
    marks_goal_done: StrictBool = False


class GoalBanCallKeyAddRequest(Contract):
    """Worker 登记一条软禁同参键（tool|argsDigest）。"""

    call_key: str = Field(min_length=1, max_length=512)


class SkillVersionRef(Contract):
    version_id: UUID
    content_digest: Digest


class ActivityAttemptResource(Contract):
    id: UUID
    created_at: datetime
    updated_at: datetime
    activity_id: UUID
    worker_id: UUID
    binding_digest: Digest
    fencing_epoch: Epoch
    lease_expires_at: datetime
    renewal_seq: NonnegativeInt
    status: AttemptStatus
    context_digest: Digest | None = None
    model_snapshot: dict | None = None
    skill_versions: list[SkillVersionRef] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime | None = None


class ActivityLease(Contract):
    lease: LeaseIdentity | None
    activity: ActivityResource | None = None
    attempt: ActivityAttemptResource | None = None
    input_artifact_ids: list[UUID] | None = None
    policy_snapshot_id: UUID | None = None
    # EXECUTE/INTEGRATE 独占工作区；未配置 RING_WORKSPACE_ROOT 时为 null。
    workspace_root: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def lease_shape(self):
        if self.lease is None:
            if any(
                value is not None
                for value in (
                    self.activity,
                    self.attempt,
                    self.input_artifact_ids,
                    self.policy_snapshot_id,
                    self.workspace_root,
                )
            ):
                raise ValueError("empty claim must not carry lease payload")
            return self
        if (
            self.activity is None
            or self.attempt is None
            or self.input_artifact_ids is None
            or self.policy_snapshot_id is None
        ):
            raise ValueError("claimed lease requires full ActivityLease payload")
        return self


class Checkpoint(Contract):
    """Checkpoint 内容（Content v3）；session_ref 为 API 附加、不进 content digest。"""

    activity_id: UUID
    attempt_id: UUID
    kind: ActivityKind
    context_digest: Digest
    completed_step_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    next_step_id: UUID | None = None
    candidate_manifest_id: UUID | None = None
    workspace_manifest_digest: Digest | None = None
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    effect_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    session_ref: str | None = Field(default=None, max_length=2000)


class CheckpointResource(Checkpoint):
    id: UUID
    project_id: UUID
    content_digest: Digest
    created_at: datetime
    schema_version: Literal[3] = 3


class CheckpointProposal(Contract):
    lease: LeaseIdentity
    checkpoint: Checkpoint


class HeartbeatRequest(Contract):
    lease: LeaseIdentity
    renewal_seq: PositiveInt


class HeartbeatResponse(Contract):
    lease_expires_at: datetime
    renewal_seq: NonnegativeInt
    control: HeartbeatControl
    # attempt 上仍 REQUESTED 的 Stop id；空则无待处理停止
    pending_stop_ids: list[UUID] = Field(default_factory=list, max_length=100)


# 本切片约定：activation_id == attempt_id（与 Runner harnessPlanAdapter 一致）；
# 独立 ActivationRef 身份尚未落地，禁止另造映射表冒充。
StopReason = Literal[
    "PAUSE",
    "CANCEL",
    "LEASE_EXPIRED",
    "FINALIZATION_RECOVERY",
    "SHUTDOWN",
    "TRUST_INVALIDATION",
]
StopStatus = Literal["REQUESTED", "CONFIRMED", "UNCONFIRMED"]
StopObservation = Literal["EXITED", "ISOLATED", "RUNNING", "UNKNOWN"]
StopReceiptDisposition = Literal["APPLIED", "PENDING_RECONCILIATION", "DUPLICATE"]


class StopRequest(Contract):
    """停止意图（05§3.6）；记录请求不等于停止成功。"""

    request_id: UUID
    activation_id: UUID
    activity_id: UUID
    attempt_id: UUID
    fencing_epoch: Epoch
    reason: StopReason
    deadline_at: datetime


class StopResource(Contract):
    id: UUID
    request: StopRequest
    status: StopStatus
    state_revision: PositiveInt
    receipt_ids: list[UUID] = Field(default_factory=list)


class StopReceipt(Contract):
    """可信宿主/Broker 签发的停止观察；模型字段不能成为证明。"""

    receipt_id: UUID
    stop_id: UUID
    activation_id: UUID
    attempt_id: UUID
    resource_instance_id: UUID
    observed_at: datetime
    observation: StopObservation
    compute_released: bool
    write_capability_revoked: bool
    proof_artifact_ids: list[UUID] = Field(default_factory=list)


class StopReceiptAccepted(Contract):
    disposition: StopReceiptDisposition


class StateRevisionConflict(Exception):
    """expected_state_revision 与当前资源不一致。"""


class InvalidGoalState(Exception):
    """Goal 当前状态不允许该控制命令。"""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


class BindingStale(Exception):
    """Activity 绑定与当前合同/配置不一致。"""


class LeaseRejected(Exception):
    """租约校验失败（过期、fencing 或 renewal_seq）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class WorkerForbidden(Exception):
    """主体不是已登记 worker，或 kinds 越权。"""


class PlanRejected(Exception):
    """计划图未通过 schema/覆盖/依赖校验。"""

    def __init__(self, message: str, *, code: str | None = None):
        self.message = message
        self.code = code
        super().__init__(message)
