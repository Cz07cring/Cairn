"""StopReason 增加 TRUST_INVALIDATION（信任传播排空）。"""

from alembic import op

revision = "0036_trust_inv_stop"
down_revision = "0035_orchestration_abandonments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 无名 inline CHECK 在 PG 上通常命名为 stops_reason_check
    op.execute("""
      ALTER TABLE stops DROP CONSTRAINT IF EXISTS stops_reason_check;
      ALTER TABLE stops ADD CONSTRAINT stops_reason_check CHECK(reason IN (
        'PAUSE','CANCEL','LEASE_EXPIRED','FINALIZATION_RECOVERY','SHUTDOWN',
        'TRUST_INVALIDATION'));
    """)


def downgrade() -> None:
    raise RuntimeError("StopReason 扩展不可就地缩回")
