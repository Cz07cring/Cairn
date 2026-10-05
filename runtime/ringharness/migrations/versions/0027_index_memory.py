"""允许 INDEX_MEMORY Activity：无 Goal/Task，target=MEMORY_INDEX。"""

from alembic import op

revision = "0027_index_memory"
down_revision = "0026_stops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 此前 catch-all 已放行 INDEX_MEMORY；显式约束 target/goal/task，与 VALIDATE_SKILL 同形。
    op.execute("""
      ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_kind_target_check;
      ALTER TABLE activities ADD CONSTRAINT activities_kind_target_check CHECK(
        (kind='PLAN' AND target_type='GOAL_PLAN' AND goal_id IS NOT NULL AND task_id IS NULL)
        OR (kind='EXECUTE' AND target_type='TASK_WORK' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='PROBE_MODEL' AND target_type='MODEL_PROFILE' AND goal_id IS NULL AND task_id IS NULL)
        OR (kind='AUDIT' AND target_type='CANDIDATE' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='VALIDATE_SKILL' AND target_type='SKILL_VERSION' AND goal_id IS NULL AND task_id IS NULL)
        OR (kind='INDEX_MEMORY' AND target_type='MEMORY_INDEX' AND goal_id IS NULL AND task_id IS NULL)
        OR kind NOT IN ('PLAN','EXECUTE','PROBE_MODEL','AUDIT','VALIDATE_SKILL','INDEX_MEMORY')
      );
    """)


def downgrade() -> None:
    raise RuntimeError("INDEX_MEMORY Activity 约束不可就地回滚")
