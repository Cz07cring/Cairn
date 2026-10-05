"""HandoffEnvelope ACK 门禁：过期版本 / digest / 角色 / UNKNOWN / 缺证据 / 工件 hash。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from control_kernel.domain.handoff_envelope import (
    HandoffAck,
    HandoffArtifactRef,
    HandoffEnvelope,
    HandoffRejected,
    assert_execute_admission_handoff,
    build_plan_to_execute_handoff,
    compute_envelope_digest,
    validate_handoff_ack,
)
from control_kernel.protocols.runtime import ExecutionBinding

DIGEST_A = "sha256:" + ("a" * 64)
DIGEST_B = "sha256:" + ("b" * 64)
DIGEST_C = "sha256:" + ("c" * 64)
DIGEST_D = "sha256:" + ("d" * 64)
DIGEST_E = "sha256:" + ("e" * 64)


def _binding(**over) -> ExecutionBinding:
    base = {
        "goal_contract_revision": 1,
        "goal_contract_digest": DIGEST_A,
        "task_contract_revision": 1,
        "task_contract_digest": DIGEST_B,
        "plan_revision": 1,
        "subject_digest": DIGEST_B,
        "policy_digest": DIGEST_C,
        "model_profile_digest": DIGEST_D,
        "skill_set_digest": DIGEST_E,
    }
    base.update(over)
    return ExecutionBinding.model_validate(base)


def _ack(envelope: HandoffEnvelope, **over) -> HandoffAck:
    base = {
        "handoff_id": envelope.handoff_id,
        "expected_envelope_digest": envelope.envelope_digest,
        "receiver_role": "EXECUTOR",
        "receiver_activity_id": None,
    }
    base.update(over)
    return HandoffAck.model_validate(base)


def _live_kwargs(envelope: HandoffEnvelope, **over) -> dict:
    base = {
        "live_plan_revision": envelope.plan_revision,
        "live_goal_contract_digest": envelope.goal_contract_digest,
        "live_policy_digest": envelope.policy_digest,
        "live_model_profile_digest": envelope.model_profile_digest,
        "live_skill_set_digest": envelope.skill_set_digest,
        "live_unknown_effect_ids": [],
        "require_evidence": True,
    }
    base.update(over)
    return base


def test_build_plan_to_execute_handoff_digest_stable() -> None:
    goal_id = uuid4()
    task_id = uuid4()
    hid = uuid4()
    binding = _binding()
    e1 = build_plan_to_execute_handoff(
        goal_id=goal_id, task_id=task_id, binding=binding, handoff_id=hid
    )
    e2 = build_plan_to_execute_handoff(
        goal_id=goal_id, task_id=task_id, binding=binding, handoff_id=hid
    )
    assert e1.envelope_digest == e2.envelope_digest == compute_envelope_digest(e1)
    assert e1.from_role == "PLANNER" and e1.to_role == "EXECUTOR"


def test_ack_happy_path() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    validate_handoff_ack(envelope, _ack(envelope), **_live_kwargs(envelope))


def test_reject_stale_plan_revision() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding(plan_revision=1)
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope, _ack(envelope), **_live_kwargs(envelope, live_plan_revision=2)
        )
    assert ei.value.code == "HANDOFF_STALE_REVISION"


def test_reject_policy_digest_mismatch() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope),
            **_live_kwargs(envelope, live_policy_digest=DIGEST_A),
        )
    assert ei.value.code == "HANDOFF_DIGEST_MISMATCH"


def test_reject_model_profile_digest_mismatch() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope),
            **_live_kwargs(envelope, live_model_profile_digest=DIGEST_A),
        )
    assert ei.value.code == "HANDOFF_DIGEST_MISMATCH"


def test_reject_skill_set_digest_mismatch() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope),
            **_live_kwargs(envelope, live_skill_set_digest=DIGEST_A),
        )
    assert ei.value.code == "HANDOFF_DIGEST_MISMATCH"


def test_reject_ack_digest_mismatch() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope, expected_envelope_digest=DIGEST_A),
            **_live_kwargs(envelope),
        )
    assert ei.value.code == "HANDOFF_DIGEST_MISMATCH"


def test_reject_tampered_envelope_digest() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    bad = envelope.model_copy(update={"envelope_digest": DIGEST_A})
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(bad, _ack(bad), **_live_kwargs(bad))
    assert ei.value.code == "HANDOFF_DIGEST_MISMATCH"


def test_reject_receiver_role_mismatch() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope, _ack(envelope, receiver_role="AUDITOR"), **_live_kwargs(envelope)
        )
    assert ei.value.code == "HANDOFF_ROLE_MISMATCH"


def test_reject_cross_role_field_leak() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope),
            **_live_kwargs(envelope, extra_payload_keys=["planner_scratchpad"]),
        )
    assert ei.value.code == "HANDOFF_ROLE_MISMATCH"


def test_reject_unknown_effects() -> None:
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=uuid4(), binding=_binding()
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope),
            **_live_kwargs(envelope, live_unknown_effect_ids=[uuid4()]),
        )
    assert ei.value.code == "HANDOFF_UNKNOWN_EFFECTS"


def test_reject_missing_evidence_without_plan_revision() -> None:
    binding = _binding(plan_revision=None)
    # ExecutionBinding 允许 plan_revision 为空；构造信封后应缺证据关闭
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(), task_id=None, binding=binding
    )
    assert envelope.plan_revision is None
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(envelope, _ack(envelope), **_live_kwargs(envelope))
    assert ei.value.code == "HANDOFF_MISSING_EVIDENCE"


def test_reject_artifact_hash_mismatch() -> None:
    art_id = uuid4()
    binding = _binding()
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(),
        task_id=uuid4(),
        binding=binding,
        artifact_refs=[HandoffArtifactRef(artifact_id=art_id, digest=DIGEST_A)],
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope),
            **_live_kwargs(envelope, known_artifact_digests={art_id: DIGEST_B}),
        )
    assert ei.value.code == "HANDOFF_DIGEST_MISMATCH"


def test_reject_missing_artifact_in_store() -> None:
    art_id = uuid4()
    envelope = build_plan_to_execute_handoff(
        goal_id=uuid4(),
        task_id=uuid4(),
        binding=_binding(),
        evidence_refs=[HandoffArtifactRef(artifact_id=art_id, digest=DIGEST_A)],
    )
    with pytest.raises(HandoffRejected) as ei:
        validate_handoff_ack(
            envelope,
            _ack(envelope),
            **_live_kwargs(envelope, known_artifact_digests={}),
        )
    assert ei.value.code == "HANDOFF_MISSING_EVIDENCE"


def test_assert_execute_admission_happy() -> None:
    binding = _binding()
    env = assert_execute_admission_handoff(
        goal_id=uuid4(),
        task_id=uuid4(),
        binding=binding,
        live_binding=binding,
        live_plan_revision=1,
        unknown_effect_ids=[],
        receiver_activity_id=uuid4(),
    )
    assert env.to_role == "EXECUTOR"


def test_assert_execute_admission_rejects_unknown() -> None:
    binding = _binding()
    with pytest.raises(HandoffRejected) as ei:
        assert_execute_admission_handoff(
            goal_id=uuid4(),
            task_id=uuid4(),
            binding=binding,
            live_binding=binding,
            live_plan_revision=1,
            unknown_effect_ids=[uuid4()],
        )
    assert ei.value.code == "HANDOFF_UNKNOWN_EFFECTS"


def test_assert_execute_admission_rejects_stale_binding() -> None:
    bound = _binding(policy_digest=DIGEST_C)
    live = _binding(policy_digest=DIGEST_A)
    with pytest.raises(HandoffRejected) as ei:
        assert_execute_admission_handoff(
            goal_id=uuid4(),
            task_id=uuid4(),
            binding=bound,
            live_binding=live,
            live_plan_revision=1,
            unknown_effect_ids=[],
        )
    assert ei.value.code == "HANDOFF_DIGEST_MISMATCH"
