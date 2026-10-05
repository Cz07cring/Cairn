"""允许 VALIDATE_SKILL Activity：无 Goal/Task，target=SKILL_VERSION。"""

from alembic import op

revision = "0022_validate_skill"
down_revision = "0021_evidence_envelopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_kind_target_check;
      ALTER TABLE activities ADD CONSTRAINT activities_kind_target_check CHECK(
        (kind='PLAN' AND target_type='GOAL_PLAN' AND goal_id IS NOT NULL AND task_id IS NULL)
        OR (kind='EXECUTE' AND target_type='TASK_WORK' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='PROBE_MODEL' AND target_type='MODEL_PROFILE' AND goal_id IS NULL AND task_id IS NULL)
        OR (kind='AUDIT' AND target_type='CANDIDATE' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='VALIDATE_SKILL' AND target_type='SKILL_VERSION' AND goal_id IS NULL AND task_id IS NULL)
        OR kind NOT IN ('PLAN','EXECUTE','PROBE_MODEL','AUDIT','VALIDATE_SKILL')
      );
    """)


def downgrade() -> None:
    raise RuntimeError("VALIDATE_SKILL Activity 约束不可就地回滚")
