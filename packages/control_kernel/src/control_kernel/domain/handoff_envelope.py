"""M4 HandoffEnvelope：角色交接契约与 ACK 门禁（digest C §16 / Top 5 #2）。

接收方必须在启动下一 activation 前 ACK；拒绝：陈旧版本、digest 不匹配、
角色不匹配、未知副作用未对账、缺证据、工件 hash 错。ACK 通过不等于 Goal DONE。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from ..protocols.goals import Digest, NonnegativeInt, PositiveInt
from ..protocols.projects import Contract
from ..protocols.runtime import ExecutionBinding

HandoffRole = Literal["PLANNER", "EXECUTOR", "AUDITOR", "FINALIZER"]

# 允许的 from→to 交接（首批：PLAN→EXECUTE；后续可扩 AUDIT/FINALIZE）
_ALLOWED_TRANSITIONS: frozenset[tuple[HandoffRole, HandoffRole]] = frozenset(
    {
        ("PLANNER", "EXECUTOR"),
        ("EXECUTOR", "AUDITOR"),
        ("AUDITOR", "FINALIZER"),
        ("EXECUTOR", "FINALIZER"),  # 无独立 AUDIT 活动时的直达终裁缝
    }
)

# 信封不得携带的跨角色私有字段名（防串话）；出现即拒收
_FORBIDDEN_CROSS_ROLE_KEYS = frozenset(
    {
        "private_reasoning",
        "planner_scratchpad",
        "hidden_tool_transcript",
        "peer_role_memory",
    }
)


class HandoffRejected(Exception):
    """交接 ACK 拒绝；code 供 Runner/Workflow 映射，不得写成 Goal DONE。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class HandoffArtifactRef(Contract):
    """交接引用的证据/工件指针（仅 id+digest；不内嵌正文）。"""

    artifact_id: UUID
    digest: Digest


class HandoffEnvelope(Contract):
    """角色间交接信封：版本/哈希/副作用对账/开放风险。"""

    handoff_id: UUID
    from_role: HandoffRole
    to_role: HandoffRole
    goal_id: UUID
    task_id: UUID | None = None
    from_activity_id: UUID | None = None
    from_attempt_id: UUID | None = None
    plan_revision: PositiveInt | None = None
    goal_contract_revision: PositiveInt | None = None
    goal_contract_digest: Digest | None = None
    task_contract_revision: PositiveInt | None = None
    task_contract_digest: Digest | None = None
    policy_digest: Digest
    model_profile_digest: Digest | None = None
    skill_set_digest: Digest | None = None
    subject_digest: Digest
    artifact_refs: list[HandoffArtifactRef] = Field(default_factory=list)
    evidence_refs: list[HandoffArtifactRef] = Field(default_factory=list)
    open_risk_codes: list[str] = Field(default_factory=list)
    known_failure_codes: list[str] = Field(default_factory=list)
    unknown_effect_ids: list[UUID] = Field(default_factory=list)
    budget_tokens_remaining: NonnegativeInt | None = None
    envelope_digest: Digest


class HandoffAck(Contract):
    """接收方 ACK：声明已核对的信封 digest 与自身角色。"""

    handoff_id: UUID
    expected_envelope_digest: Digest
    receiver_role: HandoffRole
    receiver_activity_id: UUID | None = None


def compute_envelope_digest(envelope: HandoffEnvelope) -> Digest:
    """对除 envelope_digest 外的规范字段做稳定哈希（与 Content digest 风格一致）。"""
    payload = envelope.model_dump(mode="json", exclude={"envelope_digest"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def build_plan_to_execute_handoff(
    *,
    goal_id: UUID,
    task_id: UUID | None,
    binding: ExecutionBinding,
    from_activity_id: UUID | None = None,
    from_attempt_id: UUID | None = None,
    artifact_refs: Sequence[HandoffArtifactRef] = (),
    evidence_refs: Sequence[HandoffArtifactRef] = (),
    open_risk_codes: Sequence[str] = (),
    known_failure_codes: Sequence[str] = (),
    unknown_effect_ids: Sequence[UUID] = (),
    budget_tokens_remaining: int | None = None,
    handoff_id: UUID | None = None,
) -> HandoffEnvelope:
    """由 PUBLISHED Plan 后的 ExecutionBinding 构造 PLANNER→EXECUTOR 信封。"""
    draft = HandoffEnvelope(
        handoff_id=handoff_id or uuid4(),
        from_role="PLANNER",
        to_role="EXECUTOR",
        goal_id=goal_id,
        task_id=task_id,
        from_activity_id=from_activity_id,
        from_attempt_id=from_attempt_id,
        plan_revision=binding.plan_revision,
        goal_contract_revision=binding.goal_contract_revision,
        goal_contract_digest=binding.goal_contract_digest,
        task_contract_revision=binding.task_contract_revision,
        task_contract_digest=binding.task_contract_digest,
        policy_digest=binding.policy_digest,
        model_profile_digest=binding.model_profile_digest,
        skill_set_digest=binding.skill_set_digest,
        subject_digest=binding.subject_digest,
        artifact_refs=list(artifact_refs),
        evidence_refs=list(evidence_refs),
        open_risk_codes=list(open_risk_codes),
        known_failure_codes=list(known_failure_codes),
        unknown_effect_ids=list(unknown_effect_ids),
        budget_tokens_remaining=budget_tokens_remaining,
        envelope_digest="sha256:" + ("0" * 64),
    )
    return draft.model_copy(update={"envelope_digest": compute_envelope_digest(draft)})


def validate_artifact_ref_digests(
    envelope: HandoffEnvelope,
    *,
    known_digests: Mapping[UUID, Digest],
) -> None:
    """核对信封内 artifact/evidence 指针与权威仓 digest；错 hash / 缺失均拒收。"""
    for ref in (*envelope.artifact_refs, *envelope.evidence_refs):
        if ref.artifact_id not in known_digests:
            raise HandoffRejected(
                "HANDOFF_MISSING_EVIDENCE",
                f"交接引用的工件 {ref.artifact_id} 在权威仓不存在",
            )
        if known_digests[ref.artifact_id] != ref.digest:
            raise HandoffRejected(
                "HANDOFF_DIGEST_MISMATCH",
                f"工件 {ref.artifact_id} digest 与权威仓不一致",
            )


def validate_handoff_ack(
    envelope: HandoffEnvelope,
    ack: HandoffAck,
    *,
    live_plan_revision: int | None,
    live_goal_contract_digest: Digest | None,
    live_policy_digest: Digest,
    live_model_profile_digest: Digest | None = None,
    live_skill_set_digest: Digest | None = None,
    live_unknown_effect_ids: Sequence[UUID],
    known_artifact_digests: Mapping[UUID, Digest] | None = None,
    require_evidence: bool = True,
    extra_payload_keys: Sequence[str] = (),
) -> None:
    """接收方 ACK 门禁；任一失败抛 HandoffRejected（不得启动下一 activation）。"""
    leaked = _FORBIDDEN_CROSS_ROLE_KEYS.intersection(extra_payload_keys)
    if leaked:
        raise HandoffRejected(
            "HANDOFF_ROLE_MISMATCH",
            f"交接载荷含跨角色私有字段：{', '.join(sorted(leaked))}",
        )

    if ack.handoff_id != envelope.handoff_id:
        raise HandoffRejected("HANDOFF_DIGEST_MISMATCH", "ACK handoff_id 与信封不一致")

    recomputed = compute_envelope_digest(envelope)
    if envelope.envelope_digest != recomputed:
        raise HandoffRejected(
            "HANDOFF_DIGEST_MISMATCH",
            "信封 envelope_digest 与内容不符（可能被篡改）",
        )
    if ack.expected_envelope_digest != envelope.envelope_digest:
        raise HandoffRejected("HANDOFF_DIGEST_MISMATCH", "ACK 声明的 digest 与信封不一致")

    if (envelope.from_role, envelope.to_role) not in _ALLOWED_TRANSITIONS:
        raise HandoffRejected(
            "HANDOFF_ROLE_MISMATCH",
            f"不允许的交接 {envelope.from_role}→{envelope.to_role}",
        )
    if ack.receiver_role != envelope.to_role:
        raise HandoffRejected(
            "HANDOFF_ROLE_MISMATCH",
            f"接收方角色 {ack.receiver_role} 与信封 to_role={envelope.to_role} 不匹配",
        )

    if (
        live_plan_revision is not None
        and envelope.plan_revision is not None
        and envelope.plan_revision != live_plan_revision
    ):
        raise HandoffRejected(
            "HANDOFF_STALE_REVISION",
            f"信封 plan_revision={envelope.plan_revision} 落后于 live={live_plan_revision}",
        )
    if (
        live_goal_contract_digest is not None
        and envelope.goal_contract_digest is not None
        and envelope.goal_contract_digest != live_goal_contract_digest
    ):
        raise HandoffRejected("HANDOFF_DIGEST_MISMATCH", "Goal 合同 digest 与 live 不一致")
    if envelope.policy_digest != live_policy_digest:
        raise HandoffRejected("HANDOFF_DIGEST_MISMATCH", "策略 digest 与 live 不一致")
    if (
        live_model_profile_digest is not None
        and envelope.model_profile_digest is not None
        and envelope.model_profile_digest != live_model_profile_digest
    ):
        raise HandoffRejected("HANDOFF_DIGEST_MISMATCH", "ModelProfile digest 与 live 不一致")
    if (
        live_skill_set_digest is not None
        and envelope.skill_set_digest is not None
        and envelope.skill_set_digest != live_skill_set_digest
    ):
        raise HandoffRejected("HANDOFF_DIGEST_MISMATCH", "SkillSet digest 与 live 不一致")

    unknowns = list(dict.fromkeys([*live_unknown_effect_ids, *envelope.unknown_effect_ids]))
    if unknowns:
        raise HandoffRejected(
            "HANDOFF_UNKNOWN_EFFECTS",
            f"仍有 {len(unknowns)} 个未知副作用未对账，禁止启动下一 activation",
        )

    if known_artifact_digests is not None:
        validate_artifact_ref_digests(envelope, known_digests=known_artifact_digests)

    if (
        require_evidence
        and not envelope.evidence_refs
        and not envelope.artifact_refs
        and envelope.plan_revision is None
    ):
        # PLAN→EXECUTE：plan_revision 非空即视为 Plan 发布证据已入账
        raise HandoffRejected(
            "HANDOFF_MISSING_EVIDENCE",
            "交接缺少证据/工件引用且无 plan_revision",
        )


def assert_execute_admission_handoff(
    *,
    goal_id: UUID,
    task_id: UUID | None,
    binding: ExecutionBinding,
    live_binding: ExecutionBinding,
    live_plan_revision: int | None,
    unknown_effect_ids: Sequence[UUID],
    receiver_activity_id: UUID | None = None,
) -> HandoffEnvelope:
    """EXECUTE admit 前合成 PLAN→EXECUTE 信封并自 ACK；失败则拒绝准入。"""
    if binding_fields_stale(binding, live_binding):
        raise HandoffRejected("HANDOFF_DIGEST_MISMATCH", "ExecutionBinding 与 live 绑定不一致")

    envelope = build_plan_to_execute_handoff(
        goal_id=goal_id,
        task_id=task_id,
        binding=binding,
        unknown_effect_ids=(),
        evidence_refs=[],
        artifact_refs=[],
    )
    ack = HandoffAck(
        handoff_id=envelope.handoff_id,
        expected_envelope_digest=envelope.envelope_digest,
        receiver_role="EXECUTOR",
        receiver_activity_id=receiver_activity_id,
    )
    validate_handoff_ack(
        envelope,
        ack,
        live_plan_revision=live_plan_revision,
        live_goal_contract_digest=live_binding.goal_contract_digest,
        live_policy_digest=live_binding.policy_digest,
        live_model_profile_digest=live_binding.model_profile_digest,
        live_skill_set_digest=live_binding.skill_set_digest,
        live_unknown_effect_ids=unknown_effect_ids,
        require_evidence=True,
    )
    return envelope


def binding_fields_stale(bound: ExecutionBinding, live: ExecutionBinding) -> bool:
    """与 BindingStale 对齐的字段级比较（handoff 侧复用）。"""
    return (
        bound.goal_contract_revision != live.goal_contract_revision
        or bound.goal_contract_digest != live.goal_contract_digest
        or bound.task_contract_revision != live.task_contract_revision
        or bound.task_contract_digest != live.task_contract_digest
        or bound.plan_revision != live.plan_revision
        or bound.subject_digest != live.subject_digest
        or bound.policy_digest != live.policy_digest
        or bound.model_profile_digest != live.model_profile_digest
        or bound.skill_set_digest != live.skill_set_digest
    )
