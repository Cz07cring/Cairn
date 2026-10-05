"""AB04：checkpoint / 恢复意图过期 → RECOVERY_ABANDONED 形裁决，≠DONE。"""

from datetime import UTC, datetime, timedelta

from control_kernel.domain.recovery_intent import (
    RecoveryAbandon,
    RecoveryProceed,
    evaluate_generation_recovery,
    parse_intent_deadline,
)


def test_ab04_fresh_intent_proceeds_still_not_done():
    future = datetime.now(UTC) + timedelta(hours=4)
    d = evaluate_generation_recovery(
        generation=1,
        recovery_enabled=True,
        checkpoint_schema_version=1,
        intent_valid_until=future.isoformat(),
        recovery_attempts=0,
        max_recovery_attempts=3,
        now=datetime.now(UTC),
    )
    assert isinstance(d, RecoveryProceed)
    assert d.marks_goal_done is False


def test_ab04_attempts_and_disabled_abandon():
    now = datetime.now(UTC)
    future = now + timedelta(hours=1)
    attempts = evaluate_generation_recovery(
        generation=2,
        recovery_enabled=True,
        checkpoint_schema_version=1,
        intent_valid_until=future,
        recovery_attempts=4,
        max_recovery_attempts=3,
        now=now,
    )
    assert isinstance(attempts, RecoveryAbandon)
    assert attempts.reason == "RECOVERY_ATTEMPTS_EXCEEDED"

    disabled = evaluate_generation_recovery(
        generation=2,
        recovery_enabled=False,
        checkpoint_schema_version=1,
        intent_valid_until=future,
        recovery_attempts=0,
        max_recovery_attempts=3,
        now=now,
    )
    assert isinstance(disabled, RecoveryAbandon)
    assert disabled.reason == "RECOVERY_DISABLED"


def test_ab04_generation_zero_skips_gate():
    past = datetime(2020, 1, 1, tzinfo=UTC)
    d = evaluate_generation_recovery(
        generation=0,
        recovery_enabled=False,
        checkpoint_schema_version=None,
        intent_valid_until=past,
        recovery_attempts=99,
        max_recovery_attempts=1,
        now=datetime.now(UTC),
    )
    assert isinstance(d, RecoveryProceed)


def test_parse_intent_deadline_rejects_naive_and_garbage():
    assert parse_intent_deadline("2026-01-01T00:00:00Z") is not None
    assert parse_intent_deadline("not-a-time") is None
    assert parse_intent_deadline("2026-01-01T00:00:00") is None  # 无时区
