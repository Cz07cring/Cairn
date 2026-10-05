"""VerificationObligation：SUPERSEDED 状态与 superseded_by 列（Issue #23）。"""

from alembic import op

revision = "0032_obligation_supersede"
down_revision = "0031_orchestration_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE verification_obligations
        DROP CONSTRAINT verification_obligations_status_check;

      ALTER TABLE verification_obligations
        ADD CONSTRAINT verification_obligations_status_check
          CHECK(status IN ('OPEN','ASSESSED','QUARANTINED','SUPERSEDED'));

      ALTER TABLE verification_obligations
        ADD COLUMN superseded_by_obligation_id uuid
          REFERENCES verification_obligations(id);

      CREATE INDEX verification_obligations_superseded_by
        ON verification_obligations(superseded_by_obligation_id)
        WHERE superseded_by_obligation_id IS NOT NULL;
    """)


def downgrade() -> None:
    raise RuntimeError("VerificationObligation SUPERSEDED 不可就地回滚")
